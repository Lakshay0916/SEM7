"""CrewAI agents: Repair Planner and Code Reviewer (+ a Repair Lead in the hierarchical pattern).

Two planning patterns (see AGENT_PATTERN):
* sequential   - Planner, then Reviewer, in a fixed order.
* hierarchical - a CrewAI hierarchical crew: the Repair Lead (manager agent) decides who does what and
                 delegates drafting to the Planner and checking to the Reviewer, then returns the
                 reviewed plan. Every delegation is recorded from CrewAI's event bus.

* direct     - one sequential crew; the reviewer gets the planner's draft through CrewAI task
               `context`, and task guardrails send problems back to the agent for a retry.
* blackboard - each agent runs as its own single-task crew (`run_plan_task`, `run_review_task`)
               with inputs read from the shared blackboard; the blackboard controller runs the
               guardrail and posts its feedback to the board for the planner's next try.
"""

from __future__ import annotations

import os
import time
import warnings
from pathlib import Path

os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")  # no data sent to CrewAI's cloud
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

from crewai import LLM, Agent, Crew, Process, Task  # noqa: E402

# Guardrails are closures over the repo path; CrewAI only warns that it can't checkpoint them.
warnings.filterwarnings("ignore", message="function callbacks cannot be serialized")

from app.agents.guardrails import check_draft  # noqa: E402
from app.agents.mcp_tools import crew_mcps, crew_tool_events, hint  # noqa: E402
from app.agents.models import AgentRecord, PlanDraft, PlanReview  # noqa: E402
from app.agents.retry import with_retries  # noqa: E402
from app.models.schemas import InvestigationReport, RootCauseReport  # noqa: E402
from app.observability import tracing  # noqa: E402

PLAN_TASK = """Write the smallest code change that makes the failing tests pass.

Root cause (from the analysis agents): {root_cause}
Investigation summary: {investigation}

Rules:
- Only edit source files listed under "Current content of"; never edit tests, config or dependencies.
- For each edit give the file, the function name and the COMPLETE new source of that function (from its
  `def` line), based on the current content shown in the evidence.
- Prefer restoring the behaviour the tests expect (the baseline version is often the right reference).
- If earlier attempts are listed, they were already applied and were not enough - fix what is still failing.

{evidence}"""

REVIEW_TASK = """Review the repair planner's draft against the evidence.
Approve it only if it is minimal, edits only source code, is grounded in the evidence and should make every
failing test pass. Otherwise set approved=false, list the issues and return a corrected `final_plan`
(same format: file, function, complete new function source).

{evidence}"""


PLAN_FEEDBACK = """

GUARDRAIL FEEDBACK ON YOUR PREVIOUS DRAFT (it was rejected - fix every point):
{feedback}"""

REVIEW_DRAFT = """

DRAFT TO REVIEW (posted on the blackboard by the Repair Planner):
{draft}"""

ROLES = {
    "Repair Planner": ("Propose the smallest source-code change that makes the failing tests pass",
                       "A careful Python engineer who fixes regressions without touching tests."),
    "Code Reviewer": ("Reject fixes that are not minimal, not grounded in the evidence, or that edit tests",
                      "A strict reviewer who checks every change against the failing tests and the diff."),
}


LEAD_TASK = """Deliver a reviewed fix that makes the failing tests pass. You lead two coworkers:
- the Repair Planner writes the fix: the smallest change, the COMPLETE new source of each function it edits,
  source files only (never tests, config or dependencies);
- the Code Reviewer checks a draft against the evidence and the failing tests, and approves or corrects it.
Work in this order and do not repeat a step:
1. Delegate the drafting ONCE to the Repair Planner. Its answer is the draft - do not ask it again for the same thing.
2. Delegate the review of that exact draft to the Code Reviewer (pass the full draft).
3. Only if the reviewer rejects it: send the reviewer's issues to the Repair Planner once, then have the new draft
   reviewed once. Never delegate more than 4 times in total.
They cannot see this task: pass each of them ALL the context below. Do not write the fix yourself.
Your final answer is the reviewed plan (the reviewer's corrected plan if it made one).

Root cause (from the analysis agents): {root_cause}
Investigation summary: {investigation}

{evidence}"""

LEAD_FEEDBACK = """

GUARDRAIL FEEDBACK ON THE PREVIOUS PLAN (it was rejected - make sure the new plan fixes every point):
{feedback}"""

LEAD_MAX_ITER = 8  # one iteration per delegation (+ the final answer): stops a manager that keeps re-delegating
LEAD = ("Repair Lead",
        "Deliver a minimal, reviewed fix by delegating drafting to the Repair Planner and review to the Code Reviewer",
        "An engineering lead who never writes fixes personally: splits the work, hands each coworker the full "
        "context, and only accepts a plan the reviewer has checked.")
DELEGATION_TOOLS = ("delegate_work_to_coworker", "ask_question_to_coworker")  # names as CrewAI's events report them
PATTERNS = ("sequential", "hierarchical")  # the parallel pattern lives in the ADK analysis half


def _agent(role: str, llm, scope=None, records: list[AgentRecord] | None = None, briefed: bool = False) -> Agent:
    """One CrewAI agent; with a repair `scope` it gets its MCP tools and a step callback recording tool use.

    `briefed` puts the tool instructions into the agent itself - needed when a manager writes its tasks."""
    goal, backstory = ROLES[role]
    return Agent(role=role, goal=goal, backstory=backstory + (hint(role, scope) if briefed else ""), llm=llm,
                 allow_delegation=False,
                 max_iter=8 if scope is not None else 3, verbose=False, mcps=crew_mcps(role, scope) or None)


def _llm(llm):
    model_name = llm if isinstance(llm, str) else getattr(llm, "model", type(llm).__name__)
    return (LLM(model=f"gemini/{llm}", temperature=0) if isinstance(llm, str) else llm), model_name


def _guard(extract):
    def guard(output):
        try:
            draft = extract(output)
        except Exception as exc:  # malformed output: ask for a retry
            return False, f"Output did not match the required schema: {exc}"
        problems = check_draft(draft, guard.repo_dir)
        return (False, "Fix these problems and answer again: " + "; ".join(problems)) if problems else (True, output)
    return guard


def _as(model):
    return lambda out: out.pydantic if isinstance(out.pydantic, model) else model.model_validate_json(out.raw)


def build_crew(llm, repo_dir: Path, on_task=None, scope=None, records=None) -> Crew:
    planner, reviewer = _agent("Repair Planner", llm, scope, records), _agent("Code Reviewer", llm, scope, records)
    plan_guard, review_guard = _guard(_as(PlanDraft)), _guard(lambda o: _as(PlanReview)(o).final_plan)
    plan_guard.repo_dir = review_guard.repo_dir = repo_dir
    plan = Task(name="draft repair plan", description=PLAN_TASK + hint("Repair Planner", scope), expected_output="A PlanDraft with complete function sources",
                agent=planner, output_pydantic=PlanDraft, guardrail=plan_guard, guardrail_max_retries=2)
    review = Task(name="review plan", description=REVIEW_TASK + hint("Code Reviewer", scope), expected_output="A PlanReview with the final plan",
                  agent=reviewer, context=[plan], output_pydantic=PlanReview, guardrail=review_guard,
                  guardrail_max_retries=2)
    return Crew(agents=[planner, reviewer], tasks=[plan, review], process=Process.sequential,
                verbose=False, task_callback=on_task)


def build_hierarchical_crew(llm, repo_dir: Path | None = None, scope=None, records=None,
                            feedback: list[str] | None = None) -> Crew:
    """Hierarchical crew: the Repair Lead (manager) delegates to the Planner and the Reviewer."""
    role, goal, backstory = LEAD
    lead = Agent(role=role, goal=goal, backstory=backstory, llm=llm, allow_delegation=True, verbose=False,
                 max_iter=LEAD_MAX_ITER)
    guard = None
    if repo_dir is not None:  # direct mode; on the blackboard the board's guard checks the result instead
        guard = _guard(lambda o: _as(PlanReview)(o).final_plan)
        guard.repo_dir = repo_dir
    task = Task(name="deliver reviewed repair plan", description=LEAD_TASK + (LEAD_FEEDBACK if feedback else ""),
                expected_output="A PlanReview: approved, issues, and the final plan with complete function sources",
                output_pydantic=PlanReview, guardrail=guard, guardrail_max_retries=2 if guard else 0)
    return Crew(agents=[_coworker("Repair Planner", llm, scope, records), _coworker("Code Reviewer", llm, scope, records)],
                tasks=[task], process=Process.hierarchical, manager_agent=lead, verbose=False)


def _coworker(role: str, llm, scope, records) -> Agent:
    """A crew member the manager delegates to. CrewAI loads an agent's MCP tools when a crew starts its
    own task, but a delegated task skips that step, so the tools are loaded onto the agent up front."""
    agent = _agent(role, llm, scope, records, briefed=True)
    if agent.mcps:
        agent.tools = agent.get_mcp_tools(agent.mcps)
        agent.mcps = None
    return agent


def run_hierarchical(evidence: str, investigation_summary: str, root_cause_json: str, feedback: list[str] | None,
                     llm, records: list[AgentRecord], scope=None, repo_dir: Path | None = None) -> PlanReview:
    """Hierarchical pattern: the Repair Lead plans the work and delegates it; returns the reviewed plan."""
    llm, model_name = _llm(llm)
    inputs = {"evidence": evidence, "root_cause": root_cause_json, "investigation": investigation_summary,
              "feedback": "\n".join(f"- {p}" for p in feedback or [])}
    start, seen = time.monotonic(), len(records)

    def kickoff():
        crew = build_hierarchical_crew(llm, repo_dir, scope, records, feedback)
        try:
            with tracing.span("CrewAI hierarchical crew", "crew", "crewai", model=model_name), \
                    crew_tool_events(records, LEAD[0]):
                return crew.kickoff(inputs=inputs)
        finally:
            for agent in crew.agents:  # the coworkers' MCP server processes were started up front
                agent._cleanup_mcp_clients()

    result = with_retries(kickoff, agent=LEAD[0], framework="crewai", records=records)
    usage = result.token_usage
    delegations = sum(1 for r in records[seen:] if r.framework == "crewai" and r.agent.startswith(LEAD[0] + " → "))
    records.append(AgentRecord(agent=LEAD[0], framework="crewai", model=model_name,
                               seconds=round(time.monotonic() - start, 2),
                               prompt_tokens=(usage.prompt_tokens or 0) if usage else 0,
                               output_tokens=(usage.completion_tokens or 0) if usage else 0,
                               message=f"hierarchical crew done: {delegations} delegation(s) (tokens: whole crew)"))
    return result.pydantic if isinstance(result.pydantic, PlanReview) else PlanReview.model_validate_json(result.raw)


def run_planner(evidence: str, investigation: InvestigationReport, root_cause: RootCauseReport,
                repo_dir: Path, llm, records: list[AgentRecord], scope=None, pattern: str = "sequential") -> PlanReview:
    """Run Planner -> Reviewer (or the hierarchical crew). `llm` is a model name or a CrewAI LLM."""
    if pattern == "hierarchical":
        return run_hierarchical(evidence, investigation.failure_summary, root_cause.model_dump_json(), None, llm,
                                records, scope, repo_dir)
    llm, model_name = _llm(llm)
    marks = [time.monotonic()]

    def on_task(output):
        now = time.monotonic()
        records.append(AgentRecord(agent=str(output.agent).strip() or "crew", framework="crewai", model=model_name,
                                   seconds=round(now - marks[-1], 2), message=f"task done: {output.name or 'task'}"))
        marks.append(now)

    inputs = {"evidence": evidence, "root_cause": root_cause.model_dump_json(),
              "investigation": investigation.failure_summary}
    def kickoff():
        with tracing.span("CrewAI sequential crew", "crew", "crewai", model=model_name), crew_tool_events(records):
            return build_crew(llm, repo_dir, on_task, scope, records).kickoff(inputs=inputs)

    result = with_retries(kickoff,
                          agent="crewai_planning", framework="crewai", records=records)
    usage = result.token_usage
    crew_recs = [r for r in records if r.framework == "crewai" and r.status == "ok"]
    if usage is not None and crew_recs:  # CrewAI reports usage per crew; attribute it to the last task
        crew_recs[-1].prompt_tokens = usage.prompt_tokens or 0
        crew_recs[-1].output_tokens = usage.completion_tokens or 0
        crew_recs[-1].message += " (tokens: whole crew)"
    review = result.pydantic if isinstance(result.pydantic, PlanReview) else PlanReview.model_validate_json(result.raw)
    return review


def _run_single(role: str, name: str, description: str, expected: str, output_model, inputs: dict, llm,
                records: list[AgentRecord], scope=None):
    llm, model_name = _llm(llm)
    agent = _agent(role, llm, scope, records)
    task = Task(name=name, description=description + hint(role, scope), expected_output=expected, agent=agent,
                output_pydantic=output_model)
    start = time.monotonic()
    def kickoff():
        with tracing.span(f"CrewAI crew · {role}", "crew", "crewai", model=model_name), crew_tool_events(records, role):
            return Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False).kickoff(inputs=inputs)

    result = with_retries(kickoff, agent=role, framework="crewai", records=records)
    usage = result.token_usage
    records.append(AgentRecord(agent=role, framework="crewai", model=model_name,
                               seconds=round(time.monotonic() - start, 2),
                               prompt_tokens=(usage.prompt_tokens or 0) if usage else 0,
                               output_tokens=(usage.completion_tokens or 0) if usage else 0,
                               message=f"task done: {name}"))
    return result.pydantic if isinstance(result.pydantic, output_model) else output_model.model_validate_json(result.raw)


def run_plan_task(evidence: str, investigation_summary: str, root_cause_json: str, feedback: list[str] | None,
                  llm, records: list[AgentRecord], scope=None) -> PlanDraft:
    """Repair Planner as a single-task crew (blackboard mode). `feedback` = guardrail problems, if any."""
    description = PLAN_TASK + (PLAN_FEEDBACK if feedback else "")
    inputs = {"evidence": evidence, "root_cause": root_cause_json, "investigation": investigation_summary,
              "feedback": "\n".join(f"- {p}" for p in feedback or [])}
    return _run_single("Repair Planner", "draft repair plan", description,
                       "A PlanDraft with complete function sources", PlanDraft, inputs, llm, records, scope)


def run_review_task(evidence: str, draft_json: str, llm, records: list[AgentRecord], scope=None) -> PlanReview:
    """Code Reviewer as a single-task crew (blackboard mode): reviews the draft read from the board."""
    return _run_single("Code Reviewer", "review plan", REVIEW_TASK + REVIEW_DRAFT,
                       "A PlanReview with the final plan", PlanReview,
                       {"evidence": evidence, "draft": draft_json}, llm, records, scope)
