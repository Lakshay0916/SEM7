"""AgenticStrategy: the LLM multi-agent implementation of the RepairStrategy interface.

    investigate() + root_cause()  -> Google ADK analysts (Investigator -> Root-cause analyst)
    plan()                        -> CrewAI crew (Repair Planner -> Code Reviewer)

The self-healing loop around it is unchanged: plans still go through the safety checks, human
approval, the repair branch, real tests and reflection. If an agent step fails (API down, invalid
output) and fallback is enabled, that step is done by the rule-based strategy and the event log
says so - the run is never silently faked.
"""

from __future__ import annotations

from typing import Callable

from app.agents.evidence import build_evidence
from app.agents.guardrails import check_draft, to_repair_plan
from app.agents.models import AgentRecord, PlanDraft
from app.config import settings
from app.healing.rules import RuleBasedStrategy
from app.healing.strategy import RepairContext
from app.models.schemas import InvestigationReport, RepairPlan, RootCauseReport

Analysts = Callable[[str, list[AgentRecord]], tuple[InvestigationReport, RootCauseReport]]
Planner = Callable[[str, InvestigationReport, RootCauseReport, RepairContext, list[AgentRecord]], PlanDraft]


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


class AgenticStrategy:
    name = "adk+crewai"
    kind = "llm-agent"

    def __init__(self, analysts: Analysts | None = None, planner: Planner | None = None,
                 fallback: bool | None = None) -> None:
        self.analysts = analysts or _default_analysts
        self.planner = planner or _default_planner
        self.fallback = RuleBasedStrategy() if (settings.agent_fallback if fallback is None else fallback) else None
        self.records: list[AgentRecord] = []
        self._cache: dict[int, tuple[str, RootCauseReport | None]] = {}  # attempt -> (evidence, root cause)

    def pop_records(self) -> list[AgentRecord]:
        """Monitoring records since the last call (the loop writes them to the event log)."""
        out, self.records = self.records, []
        return out

    def _fail(self, step: str, exc: Exception) -> None:
        if self.fallback is None:
            raise exc
        self.records.append(AgentRecord(agent=step, framework="system", status="fallback",
                                        message=f"{type(exc).__name__}: {str(exc)[:200]} - using rule-based {step}"))

    # ------------------------------------------------------------------ interface

    def investigate(self, ctx: RepairContext) -> InvestigationReport:
        evidence = build_evidence(ctx)
        try:
            inv, rc = self.analysts(evidence, self.records)
        except Exception as exc:
            self._fail("analysis", exc)
            self._cache[ctx.attempt] = (evidence, None)
            return self.fallback.investigate(ctx)
        self._cache[ctx.attempt] = (evidence, rc)
        return inv

    def root_cause(self, ctx: RepairContext, investigation: InvestigationReport) -> RootCauseReport:
        _, rc = self._cache.get(ctx.attempt, ("", None))
        return rc if rc is not None else self.fallback.root_cause(ctx, investigation)

    def plan(self, ctx: RepairContext, investigation: InvestigationReport,
             root_cause: RootCauseReport) -> RepairPlan | None:
        evidence = self._cache.get(ctx.attempt, (None,))[0] or build_evidence(ctx)
        try:
            draft = self.planner(evidence, investigation, root_cause, ctx, self.records)
            problems = check_draft(draft, ctx.repo_dir)
            if problems:
                self.records.append(AgentRecord(agent="plan guardrail", framework="system", status="blocked",
                                                message="; ".join(problems)[:300]))
                raise ValueError("agent plan failed the guardrail checks")
            return to_repair_plan(draft, ctx, root_cause, self.name, self.kind)
        except Exception as exc:
            self._fail("planning", exc)
            return self.fallback.plan(ctx, investigation, root_cause)
