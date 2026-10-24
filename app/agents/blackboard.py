"""Blackboard architecture: agents share context through one shared, append-only board.

    ┌──────────────────────────── Blackboard (per repair attempt) ────────────────────────────┐
    │ evidence → investigation → root_cause → plan_draft → [plan_check] → review                │
    └───────▲───────────────▲──────────────▲──────────────────▲───────────────▲─────────────────┘
            │ orchestrator  │ ADK          │ ADK              │ CrewAI        │ CrewAI
                             investigator   root-cause         planner         reviewer

Agents never call each other. Each knowledge source declares what it needs from the board and
what it contributes; the controller repeatedly picks a source whose inputs are on the board and
whose output is not, runs it and posts the result. Guards check contributions; a rejected
contribution is retracted and its feedback posted, so the producing agent runs again with it.
Every post is kept (with author, framework and time), and the loop persists the board to the
workflow store, so the whole collaboration is auditable afterwards.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.agents.models import AgentRecord


class BlackboardEntry(BaseModel):
    seq: int
    key: str
    author: str
    framework: str  # system | adk | crewai | guardrail | rule-based
    attempt: int
    summary: str
    content: Any = None
    retracted: bool = False
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Blackboard:
    def __init__(self, attempt: int = 1) -> None:
        self.attempt = attempt
        self.entries: list[BlackboardEntry] = []
        self._persisted = 0

    def post(self, key: str, content: Any, author: str, framework: str, summary: str = "") -> BlackboardEntry:
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="json")
        entry = BlackboardEntry(seq=len(self.entries) + 1, key=key, author=author, framework=framework,
                                attempt=self.attempt, summary=summary[:300], content=content)
        self.entries.append(entry)
        return entry

    def _latest(self, key: str) -> BlackboardEntry | None:
        return next((e for e in reversed(self.entries) if e.key == key and not e.retracted), None)

    def has(self, key: str) -> bool:
        return self._latest(key) is not None

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._latest(key)
        return entry.content if entry else default

    def retract(self, key: str) -> None:
        if entry := self._latest(key):
            entry.retracted = True

    def count(self, key: str) -> int:
        return sum(e.key == key for e in self.entries)

    def drain(self) -> list[BlackboardEntry]:
        """Entries not yet persisted (the loop saves them to the workflow store)."""
        out = self.entries[self._persisted:]
        self._persisted = len(self.entries)
        return out


@dataclass
class KnowledgeSource:
    """An agent seen from the board: what it reads, what it writes, and how to run it."""

    name: str
    framework: str
    needs: tuple[str, ...]
    produces: str
    run: Callable[[Blackboard, list[AgentRecord]], tuple[Any, str]]  # -> (content, one-line summary)


@dataclass
class Guard:
    """Checks a contribution; problems -> retract it and post feedback for the producer's next try."""

    key: str
    check: Callable[[Any], list[str]]
    name: str = "guardrail"


class BlackboardStuck(RuntimeError):
    pass


@dataclass
class BlackboardController:
    sources: list[KnowledgeSource]
    guards: list[Guard] = field(default_factory=list)
    max_retries: int = 2

    def ready(self, bb: Blackboard) -> list[KnowledgeSource]:
        return [ks for ks in self.sources if not bb.has(ks.produces) and all(bb.has(n) for n in ks.needs)]

    def run_until(self, bb: Blackboard, goal: str, records: list[AgentRecord]) -> Any:
        while not bb.has(goal):
            ready = self.ready(bb)
            if not ready:
                missing = [ks.produces for ks in self.sources if not bb.has(ks.produces)]
                raise BlackboardStuck(f"no agent can contribute towards {goal!r} (missing: {', '.join(missing)})")
            ks = ready[0]
            start, seen = time.monotonic(), len(records)
            content, summary = ks.run(bb, records)
            for r in records[seen:]:  # tool calls the agent made while working, in order
                if r.framework == "mcp":
                    bb.post(f"tool:{r.agent.split(' → ')[-1]}", r.message, author=ks.name, framework="mcp",
                            summary=f"{r.status} in {r.seconds:.2f}s - {r.message}")
            bb.post(ks.produces, content, author=ks.name, framework=ks.framework, summary=summary)
            for guard in (g for g in self.guards if g.key == ks.produces):
                problems = guard.check(bb.get(ks.produces))
                if not problems:
                    continue
                tries = bb.count(f"feedback:{ks.produces}") + 1
                bb.retract(ks.produces)
                bb.post(f"feedback:{ks.produces}", problems, author=guard.name, framework="guardrail",
                        summary=f"rejected {ks.produces} (try {tries}): " + "; ".join(problems))
                records.append(AgentRecord(agent=guard.name, framework="system", status="blocked",
                                           seconds=round(time.monotonic() - start, 2),
                                           message=f"{ks.produces} rejected: " + "; ".join(problems)[:250]))
                if tries > self.max_retries:
                    raise ValueError(f"{ks.produces} still invalid after {self.max_retries} retries: "
                                     + "; ".join(problems))
        return bb.get(goal)
