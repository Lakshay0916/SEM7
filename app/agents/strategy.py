"""AgenticStrategy: the LLM multi-agent implementation of the RepairStrategy interface.

    investigate() + root_cause()  -> Google ADK agents (Investigator, Root-cause analyst)
    plan()                        -> CrewAI agents (Repair Planner, Code Reviewer)

How the agents share context (AGENT_MESSAGING):
* blackboard (default) - every agent reads from and posts to one shared Blackboard; a controller
  decides who runs next from what is on the board (app/agents/blackboard.py). Persisted per attempt.
* direct - point-to-point hand-off: ADK session state inside a SequentialAgent, then CrewAI task
  context inside one crew. Kept for comparison with the blackboard.

The self-healing loop around it is unchanged: plans still go through the safety checks, human
approval, the repair branch, real tests and reflection. If an agent step fails (API down, invalid
output) and fallback is enabled, that step is done by the rule-based strategy and the event log
says so - the run is never silently faked.
"""

from __future__ import annotations

import json
from typing import Callable

from app.agents.blackboard import Blackboard, BlackboardController, BlackboardEntry, Guard, KnowledgeSource
from app.agents.evidence import build_evidence
from app.agents.guardrails import check_draft, to_repair_plan
from app.agents.models import AgentRecord, PlanDraft, PlanReview
from app.config import settings
from app.healing.rules import RuleBasedStrategy
from app.healing.strategy import RepairContext
from app.models.schemas import InvestigationReport, RepairPlan, RootCauseReport

Analysts = Callable[[str, list[AgentRecord]], tuple[InvestigationReport, RootCauseReport]]
Planner = Callable[[str, InvestigationReport, RootCauseReport, RepairContext, list[AgentRecord]], PlanDraft]
MESSAGING = ("blackboard", "direct")


# --------------------------------------------------------------------------- direct (point-to-point)


def _default_analysts(evidence: str, records: list[AgentRecord]):
    from app.agents.adk_analysts import run_analysts

    return run_analysts(evidence, settings.agent_model, records)


def _default_planner(evidence, investigation, root_cause, ctx, records):
    from app.agents.crew_planner import run_planner

    review = run_planner(evidence, investigation, root_cause, ctx.repo_dir, settings.agent_model, records)
    if not review.approved:
        records.append(AgentRecord(agent="Code Reviewer", framework="crewai", status="ok",
                                   message="reviewer corrected the draft: " + "; ".join(review.issues)[:300]))
    return review.final_plan


# --------------------------------------------------------------------------- blackboard


def default_sources(model: str | None = None) -> list[KnowledgeSource]:
    """The four agents as blackboard knowledge sources (ADK analysts, CrewAI planner and reviewer)."""
    model = model or settings.agent_model

    def investigator(bb, records):
        from app.agents.adk_analysts import run_agent

        inv = run_agent("investigator", {"evidence": bb.get("evidence")}, model, records)
        return inv, inv.failure_summary

    def root_cause_analyst(bb, records):
        from app.agents.adk_analysts import run_agent

        rc = run_agent("root_cause_analyst", {"evidence": bb.get("evidence"),
                                              "investigation": json.dumps(bb.get("investigation"))}, model, records)
        return rc, f"{rc.root_cause} (confidence {rc.confidence:.0%})"

    def planner(bb, records):
        from app.agents.crew_planner import run_plan_task

        draft = run_plan_task(bb.get("evidence"), bb.get("investigation")["failure_summary"],
                              json.dumps(bb.get("root_cause")), bb.get("feedback:plan_draft"), model, records)
        return draft, draft.summary

    def reviewer(bb, records):
        from app.agents.crew_planner import run_review_task

        review = run_review_task(bb.get("evidence"), json.dumps(bb.get("plan_draft")), model, records)
        verdict = "approved" if review.approved else "corrected: " + "; ".join(review.issues)
        return review, f"{verdict} - {review.final_plan.summary}"

    return [
        KnowledgeSource("investigator", "adk", ("evidence",), "investigation", investigator),
        KnowledgeSource("root_cause_analyst", "adk", ("evidence", "investigation"), "root_cause", root_cause_analyst),
        KnowledgeSource("Repair Planner", "crewai", ("evidence", "root_cause"), "plan_draft", planner),
        KnowledgeSource("Code Reviewer", "crewai", ("evidence", "root_cause", "plan_draft"), "review", reviewer),
    ]


def plan_guards(repo_dir) -> list[Guard]:
    return [
        Guard("plan_draft", lambda c: check_draft(PlanDraft.model_validate(c), repo_dir), name="plan guardrail"),
        Guard("review", lambda c: check_draft(PlanReview.model_validate(c).final_plan, repo_dir),
              name="review guardrail"),
    ]


# --------------------------------------------------------------------------- strategy


class AgenticStrategy:
    name = "adk+crewai"
    kind = "llm-agent"

    def __init__(self, analysts: Analysts | None = None, planner: Planner | None = None,
                 fallback: bool | None = None, messaging: str | None = None,
                 sources: list[KnowledgeSource] | None = None) -> None:
        self.messaging = (messaging or settings.agent_messaging).lower()
        if self.messaging not in MESSAGING:
            raise ValueError(f"unknown AGENT_MESSAGING {self.messaging!r} (use {' or '.join(MESSAGING)})")
        self.analysts = analysts or _default_analysts
        self.planner = planner or _default_planner
        self.sources = sources
        self.fallback = RuleBasedStrategy() if (settings.agent_fallback if fallback is None else fallback) else None
        self.records: list[AgentRecord] = []
        self.boards: dict[int, Blackboard] = {}
        self._cache: dict[int, tuple[str, RootCauseReport | None]] = {}  # attempt -> (evidence, root cause)

    def pop_records(self) -> list[AgentRecord]:
        """Monitoring records since the last call (the loop writes them to the event log)."""
        out, self.records = self.records, []
        return out

    def pop_blackboard(self) -> list[BlackboardEntry]:
        """Blackboard posts since the last call (the loop persists them with the workflow)."""
        return [e for bb in self.boards.values() for e in bb.drain()]

    def _fail(self, step: str, exc: Exception) -> None:
        if self.fallback is None:
            raise exc
        self.records.append(AgentRecord(agent=step, framework="system", status="fallback",
                                        message=f"{type(exc).__name__}: {str(exc)[:200]} - using rule-based {step}"))

    def _controller(self, ctx: RepairContext) -> BlackboardController:
        return BlackboardController(self.sources or default_sources(), plan_guards(ctx.repo_dir))

    # ------------------------------------------------------------------ interface

    def investigate(self, ctx: RepairContext) -> InvestigationReport:
        evidence = build_evidence(ctx)
        try:
            if self.messaging == "blackboard":
                bb = self.boards[ctx.attempt] = Blackboard(ctx.attempt)
                bb.post("evidence", evidence, author="orchestrator", framework="system",
                        summary=f"evidence package for attempt {ctx.attempt}: "
                                f"{len(ctx.failing_tests)} failing test(s), {len(evidence)} chars")
                self._controller(ctx).run_until(bb, "root_cause", self.records)
                inv = InvestigationReport.model_validate(bb.get("investigation"))
                rc = RootCauseReport.model_validate(bb.get("root_cause"))
            else:
                inv, rc = self.analysts(evidence, self.records)
        except Exception as exc:
            self._fail("analysis", exc)
            self._cache[ctx.attempt] = (evidence, None)
            inv = self.fallback.investigate(ctx)
            if bb := self.boards.get(ctx.attempt):
                bb.post("investigation", inv, author="rule-based (fallback)", framework="rule-based",
                        summary=inv.failure_summary)
            return inv
        self._cache[ctx.attempt] = (evidence, rc)
        return inv

    def root_cause(self, ctx: RepairContext, investigation: InvestigationReport) -> RootCauseReport:
        _, rc = self._cache.get(ctx.attempt, ("", None))
        if rc is not None:
            return rc
        rc = self.fallback.root_cause(ctx, investigation)
        if bb := self.boards.get(ctx.attempt):
            bb.post("root_cause", rc, author="rule-based (fallback)", framework="rule-based", summary=rc.root_cause)
        return rc

    def plan(self, ctx: RepairContext, investigation: InvestigationReport,
             root_cause: RootCauseReport) -> RepairPlan | None:
        evidence = self._cache.get(ctx.attempt, (None,))[0] or build_evidence(ctx)
        try:
            if self.messaging == "blackboard":
                review = PlanReview.model_validate(self._controller(ctx).run_until(self.boards[ctx.attempt],
                                                                                   "review", self.records))
                draft = review.final_plan
            else:
                draft = self.planner(evidence, investigation, root_cause, ctx, self.records)
            problems = check_draft(draft, ctx.repo_dir)
            if problems:
                self.records.append(AgentRecord(agent="plan guardrail", framework="system", status="blocked",
                                                message="; ".join(problems)[:300]))
                raise ValueError("agent plan failed the guardrail checks")
            return to_repair_plan(draft, ctx, root_cause, self.name, self.kind)
        except Exception as exc:
            self._fail("planning", exc)
            plan = self.fallback.plan(ctx, investigation, root_cause)
            if plan and (bb := self.boards.get(ctx.attempt)):
                bb.post("review", {"approved": True, "issues": [], "fallback_plan": plan.summary},
                        author="rule-based (fallback)", framework="rule-based", summary=plan.summary)
            return plan
