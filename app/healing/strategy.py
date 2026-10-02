"""The pluggable repair-strategy interface.

The self-healing loop (approval -> repair branch -> real tests -> reflection ->
PR) is strategy-agnostic. A strategy only has to answer three questions, which
map one-to-one onto the planned agents:

    investigate()  -> Investigation Agent   (what failed?)
    root_cause()   -> Root Cause Agent      (why?  - the LLM version will use RAG)
    plan()         -> Solution Planner      (exact proposed changes)

Today `RuleBasedStrategy` implements it deterministically (IDT / DevOps scope).
An LLM-backed strategy can be dropped in later without touching the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.models.schemas import (
    FailureEvent,
    InvestigationReport,
    RepairPlan,
    Reflection,
    RootCauseReport,
    TestReport,
)


@dataclass
class RepairContext:
    """Everything a strategy may look at. Strategies must not invent evidence beyond this."""

    repo_dir: Path
    base_branch: str  # healthy baseline, e.g. "main"
    failure: FailureEvent  # the original CI failure (diff = developer's change)
    current_report: TestReport  # latest real test result (original, or after a failed repair)
    attempt: int  # 1-based attempt number about to be planned
    previous_plans: list[RepairPlan] = field(default_factory=list)
    reflections: list[Reflection] = field(default_factory=list)

    @property
    def failing_tests(self) -> list[str]:
        return [f.node_id for f in self.current_report.failures]


class RepairStrategy(Protocol):
    name: str
    kind: str  # "rule-based" | "llm-agent"

    def investigate(self, ctx: RepairContext) -> InvestigationReport: ...

    def root_cause(self, ctx: RepairContext, investigation: InvestigationReport) -> RootCauseReport: ...

    def plan(
        self, ctx: RepairContext, investigation: InvestigationReport, root_cause: RootCauseReport
    ) -> RepairPlan | None:
        """Return a plan, or None when the strategy has nothing (more) to offer."""
        ...
