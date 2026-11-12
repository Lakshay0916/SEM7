"""AgenticStrategy: the LLM multi-agent implementation of the RepairStrategy interface.

    investigate() + root_cause()  -> Google ADK agents (Investigator, Root-cause analyst)
    plan()                        -> CrewAI agents (Repair Planner, Code Reviewer)

How the agents share context (AGENT_MESSAGING):
* blackboard (default) - every agent reads from and posts to one shared Blackboard; a controller
  decides who runs next from what is on the board (app/agents/blackboard.py). Persisted per attempt.
* direct - point-to-point hand-off: ADK session state inside a SequentialAgent, then CrewAI task
  context inside one crew. Kept for comparison with the blackboard.

How the agents are organised (AGENT_PATTERN), in either messaging mode:
* sequential (default) - Investigator -> Root-cause -> Planner -> Reviewer, one after another.
* parallel     - three specialist investigators at once (ADK ParallelAgent), merged by an Investigation Lead.
* hierarchical - a Repair Lead manager agent delegates drafting and review (CrewAI hierarchical process).

The self-healing loop around it is unchanged: plans still go through the safety checks, human
approval, the repair branch, real tests and reflection. If an agent step fails (API down, invalid
output) and fallback is enabled, that step is done by the rule-based strategy and the event log
says so - the run is never silently faked.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from app.agents.blackboard import Blackboard, BlackboardController, BlackboardEntry, Guard, KnowledgeSource
from app.agents.evidence import build_evidence
from app.agents.guardrails import check_draft, to_repair_plan
from app.agents.models import AgentRecord, PlanDraft, PlanReview
from app.mcp_servers.tools import RepairScope
from app.config import settings
from app.healing.rules import RuleBasedStrategy
from app.healing.strategy import RepairContext
from app.models.schemas import InvestigationReport, RepairPlan, RootCauseReport
from app.observability import tracing
from app.security.input_guard import InputBlocked, Verdict, check_text, spotlight

Analysts = Callable[[str, list[AgentRecord]], tuple[InvestigationReport, RootCauseReport]]
Planner = Callable[[str, InvestigationReport, RootCauseReport, RepairContext, list[AgentRecord]], PlanDraft]
MESSAGING = ("blackboard", "direct")
PATTERNS = ("sequential", "parallel", "hierarchical")


# --------------------------------------------------------------------------- direct (point-to-point)


def scope_of(ctx: RepairContext) -> RepairScope | None:
    """The repair the agents' MCP tools are scoped to (None when tools are switched off)."""
    if not settings.agent_tools:
        return None
    return RepairScope(ctx.repo_dir, ctx.failure.diff.base_sha, ctx.failure.commit_sha)


def _default_analysts(evidence: str, records: list[AgentRecord], scope=None, pattern: str = "sequential"):
    from app.agents.adk_analysts import run_analysts

    return run_analysts(evidence, settings.agent_model, records, scope, pattern)


def _default_planner(evidence, investigation, root_cause, ctx, records, pattern: str = "sequential"):
    from app.agents.crew_planner import run_planner

    review = run_planner(evidence, investigation, root_cause, ctx.repo_dir, settings.agent_model, records,
                         scope_of(ctx), pattern)
    if not review.approved:
        records.append(AgentRecord(agent="Code Reviewer", framework="crewai", status="ok",
                                   message="reviewer corrected the draft: " + "; ".join(review.issues)[:300]))
    return review.final_plan


# --------------------------------------------------------------------------- blackboard


def default_sources(model: str | None = None, scope: RepairScope | None = None,
                    pattern: str = "sequential") -> list[KnowledgeSource]:
    """The agents as blackboard knowledge sources (ADK analysts, CrewAI planner and reviewer).

    The pattern decides which sources exist: `parallel` swaps the investigator for the parallel
    investigation (its specialists' findings are posted too); `hierarchical` swaps planner + reviewer for
    the Repair Lead's crew. With a `scope`, each agent can call its MCP tools (app/agents/mcp_tools.py)."""
    model = model or settings.agent_model

    def investigator(bb, records):
        from app.agents.adk_analysts import run_agent

        inv = run_agent("investigator", {"evidence": bb.get("evidence")}, model, records, scope)
        return inv, inv.failure_summary

    def root_cause_analyst(bb, records):
        from app.agents.adk_analysts import run_agent

        rc = run_agent("root_cause_analyst", {"evidence": bb.get("evidence"),
                                              "investigation": json.dumps(bb.get("investigation"))}, model, records,
                       scope)
        return rc, f"{rc.root_cause} (confidence {rc.confidence:.0%})"

    def planner(bb, records):
        from app.agents.crew_planner import run_plan_task

        draft = run_plan_task(bb.get("evidence"), bb.get("investigation")["failure_summary"],
                              json.dumps(bb.get("root_cause")), bb.get("feedback:plan_draft"), model, records, scope)
        return draft, draft.summary

    def reviewer(bb, records):
        from app.agents.crew_planner import run_review_task

        review = run_review_task(bb.get("evidence"), json.dumps(bb.get("plan_draft")), model, records, scope)
        return review, review_summary(review)

    def parallel_investigation(bb, records):
        from app.agents.adk_analysts import run_parallel_investigation

        inv, findings = run_parallel_investigation(bb.get("evidence"), model, records, scope)
        post_findings(bb, findings)
        return inv, inv.failure_summary

    def lead(bb, records):
        from app.agents.crew_planner import run_hierarchical

        review = run_hierarchical(bb.get("evidence"), bb.get("investigation")["failure_summary"],
                                  json.dumps(bb.get("root_cause")), bb.get("feedback:review"), model, records, scope)
        return review, review_summary(review)

    return arrange(pattern, investigator, root_cause_analyst, planner, reviewer, parallel_investigation, lead)


def post_findings(bb: Blackboard, findings: dict) -> None:
    """The parallel specialists' findings go on the board too, so each angle stays visible and auditable."""
    for name, f in findings.items():
        f = f if isinstance(f, dict) else f.model_dump(mode="json")
        bb.post(f"finding:{name}", f, author=name, framework="adk",
                summary=f"{f.get('focus', name)}: " + "; ".join(f.get("facts", [])[:2])[:200])


def review_summary(review: PlanReview) -> str:
    return ("approved" if review.approved else "corrected: " + "; ".join(review.issues)) + \
        f" - {review.final_plan.summary}"


def arrange(pattern: str, investigator, root_cause_analyst, planner, reviewer, parallel_investigation,
            lead) -> list[KnowledgeSource]:
    """The knowledge sources for one pattern (shared by in-process and A2A agents)."""
    if pattern not in PATTERNS:
        raise ValueError(f"unknown AGENT_PATTERN {pattern!r} (use {', '.join(PATTERNS)})")
    first = (KnowledgeSource("parallel investigators", "adk", ("evidence",), "investigation", parallel_investigation)
             if pattern == "parallel" else
             KnowledgeSource("investigator", "adk", ("evidence",), "investigation", investigator))
    analysis = [first, KnowledgeSource("root_cause_analyst", "adk", ("evidence", "investigation"), "root_cause",
                                       root_cause_analyst)]
    if pattern == "hierarchical":
        return analysis + [KnowledgeSource("Repair Lead", "crewai", ("evidence", "root_cause"), "review", lead)]
    return analysis + [
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


class _Phase:
    """Context manager: activate the attempt's trace and wrap the block in a phase span."""

    def __init__(self, trace, root, name):
        self.trace, self.root, self.name = trace, root, name

    def __enter__(self):
        self._activation = tracing.activate(self.trace, self.root.span_id)
        self._activation.__enter__()
        self._span = tracing.span(self.name, "phase", "system")
        self._span.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            self._span.__exit__(*exc)
        finally:
            self._activation.__exit__(*exc)
        return False


class AgenticStrategy:
    name = "adk+crewai"
    kind = "llm-agent"

    def __init__(self, analysts: Analysts | None = None, planner: Planner | None = None,
                 fallback: bool | None = None, messaging: str | None = None,
                 sources: list[KnowledgeSource] | None = None, transport: str | None = None,
                 pattern: str | None = None) -> None:
        self.messaging = (messaging or settings.agent_messaging).lower()
        self.pattern = (pattern or settings.agent_pattern).lower()
        if self.pattern not in PATTERNS:
            raise ValueError(f"unknown AGENT_PATTERN {self.pattern!r} (use {', '.join(PATTERNS)})")
        self.transport = (transport or settings.agent_transport).lower()
        if self.transport not in ("local", "a2a"):
            raise ValueError(f"unknown AGENT_TRANSPORT {self.transport!r} (use local or a2a)")
        if self.messaging not in MESSAGING:
            raise ValueError(f"unknown AGENT_MESSAGING {self.messaging!r} (use {' or '.join(MESSAGING)})")
        self.analysts = analysts or _default_analysts
        self.planner = planner or _default_planner
        self.remote = None
        if self.transport == "a2a" and analysts is None and planner is None:
            from app.a2a_agents.remote import RemoteDirect

            self.remote = RemoteDirect()
        self.sources = sources
        self.fallback = RuleBasedStrategy() if (settings.agent_fallback if fallback is None else fallback) else None
        self.records: list[AgentRecord] = []
        self.boards: dict[int, Blackboard] = {}
        self.blocked: dict[int, Verdict] = {}  # attempts whose evidence the input guardrail kept from the agents
        self.traces: dict[int, tuple[tracing.Trace, tracing.Span]] = {}  # attempt -> (trace, its root span)
        self.finished: list[tuple[int, list[dict]]] = []                # traces ready to be saved
        self._cache: dict[int, tuple[str, RootCauseReport | None]] = {}  # attempt -> (evidence, root cause)

    def pop_records(self) -> list[AgentRecord]:
        """Monitoring records since the last call (the loop writes them to the event log)."""
        out, self.records = self.records, []
        return out

    def pop_traces(self) -> list[tuple[int, list[dict]]]:
        """Finished attempts' traces (attempt, spans) since the last call; the loop saves them."""
        out, self.finished = self.finished, []
        return out

    def _trace(self, ctx: RepairContext) -> tuple[tracing.Trace, tracing.Span]:
        if ctx.attempt not in self.traces:
            trace = tracing.Trace()
            root = trace.open(f"repair attempt {ctx.attempt}", "attempt", None, "system", strategy=self.name,
                              pattern=self.pattern, messaging=self.messaging, transport=self.transport)
            self.traces[ctx.attempt] = (trace, root)
        return self.traces[ctx.attempt]

    def _phase(self, ctx: RepairContext, name: str):
        """Run one interface method as a phase span of this attempt's trace."""
        trace, root = self._trace(ctx)
        return _Phase(trace, root, name)

    def pop_blackboard(self) -> list[BlackboardEntry]:
        """Blackboard posts since the last call (the loop persists them with the workflow)."""
        return [e for bb in self.boards.values() for e in bb.drain()]

    def _fail(self, step: str, exc: Exception) -> None:
        if self.fallback is None:
            raise exc
        self.records.append(AgentRecord(agent=step, framework="system", status="fallback",
                                        message=f"{type(exc).__name__}: {str(exc)[:200]} - using rule-based {step}"))

    def _controller(self, ctx: RepairContext) -> BlackboardController:
        if self.sources:
            sources = self.sources
        elif self.transport == "a2a":
            from app.a2a_agents.remote import remote_sources

            sources = remote_sources(scope_of(ctx), pattern=self.pattern)
        else:
            sources = default_sources(scope=scope_of(ctx), pattern=self.pattern)
        return BlackboardController(sources, plan_guards(ctx.repo_dir))

    # ------------------------------------------------------------------ interface

    def _screen(self, ctx: RepairContext, evidence: str) -> tuple[str, Verdict]:
        """Input guardrail: scan the evidence for prompt injection before any agent reads it."""
        with tracing.span("input guardrail", "guardrail", "system") as gs:
            verdict = check_text(evidence, "evidence")
            if gs is not None:
                gs.attributes.update(verdict=verdict.action, findings=verdict.summary[:200])
        status = {"allow": "ok", "flag": "flagged", "block": "blocked"}[verdict.action]
        self.records.append(AgentRecord(agent="input guardrail", framework="system", status=status,
                                        message=f"{verdict.action}: {verdict.summary}"[:400]))
        if verdict.action == "block":
            self.blocked[ctx.attempt] = verdict
        return spotlight(evidence, verdict), verdict

    def investigate(self, ctx: RepairContext) -> InvestigationReport:
        with self._phase(ctx, "investigate"):
            return self._investigate(ctx)

    def root_cause(self, ctx: RepairContext, investigation: InvestigationReport) -> RootCauseReport:
        with self._phase(ctx, "root cause"):
            return self._root_cause(ctx, investigation)

    def plan(self, ctx: RepairContext, investigation: InvestigationReport,
             root_cause: RootCauseReport) -> RepairPlan | None:
        try:
            with self._phase(ctx, "plan"):
                return self._plan(ctx, investigation, root_cause)
        finally:  # the attempt's analysis is over: close its trace and hand it to the loop
            trace, root = self.traces.pop(ctx.attempt, (None, None))
            if trace is not None:
                root.end = time.time()
                self.finished.append((ctx.attempt, trace.drain()))

    def _investigate(self, ctx: RepairContext) -> InvestigationReport:
        evidence, verdict = self._screen(ctx, build_evidence(ctx))
        if self.messaging == "blackboard":
            bb = self.boards[ctx.attempt] = Blackboard(ctx.attempt)
            bb.post("evidence", evidence, author="orchestrator", framework="system",
                    summary=f"evidence package for attempt {ctx.attempt}: "
                            f"{len(ctx.failing_tests)} failing test(s), {len(evidence)} chars")
            if verdict.findings:
                bb.post("guardrail:input", verdict.to_dict(), author="input guardrail", framework="guardrail",
                        summary=f"{verdict.action}: {verdict.summary}"[:300])
        try:
            if verdict.action == "block":
                raise InputBlocked(f"input guardrail kept the evidence from the agents ({verdict.summary})"[:300])
            if self.messaging == "blackboard":
                self._controller(ctx).run_until(bb, "root_cause", self.records)
                inv = InvestigationReport.model_validate(bb.get("investigation"))
                rc = RootCauseReport.model_validate(bb.get("root_cause"))
            else:
                if self.remote is not None:
                    inv, rc = self.remote.analyse(evidence, self.records, scope_of(ctx), ctx.repo_dir, self.pattern)
                elif self.analysts is _default_analysts:
                    inv, rc = self.analysts(evidence, self.records, scope_of(ctx), self.pattern)
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

    def _root_cause(self, ctx: RepairContext, investigation: InvestigationReport) -> RootCauseReport:
        _, rc = self._cache.get(ctx.attempt, ("", None))
        if rc is not None:
            return rc
        rc = self.fallback.root_cause(ctx, investigation)
        if bb := self.boards.get(ctx.attempt):
            bb.post("root_cause", rc, author="rule-based (fallback)", framework="rule-based", summary=rc.root_cause)
        return rc

    def _plan(self, ctx: RepairContext, investigation: InvestigationReport,
              root_cause: RootCauseReport) -> RepairPlan | None:
        evidence = self._cache.get(ctx.attempt, (None,))[0] or build_evidence(ctx)
        try:
            if ctx.attempt in self.blocked:
                raise InputBlocked("input guardrail blocked this attempt's evidence; the agents were not run")
            if self.messaging == "blackboard":
                review = PlanReview.model_validate(self._controller(ctx).run_until(self.boards[ctx.attempt],
                                                                                   "review", self.records))
                draft = review.final_plan
            elif self.remote is not None:
                draft = self.remote.plan(ctx.repo_dir)  # the CrewAI service already planned during the hand-off
            elif self.planner is _default_planner:
                draft = self.planner(evidence, investigation, root_cause, ctx, self.records, self.pattern)
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
