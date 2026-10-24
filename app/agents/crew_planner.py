"""CrewAI agents: Repair Planner and Code Reviewer.

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
from app.agents.models import AgentRecord, PlanDraft, PlanReview  # noqa: E402
from app.agents.retry import with_retries  # noqa: E402
from app.models.schemas import InvestigationReport, RootCauseReport  # noqa: E402

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


def _agent(role: str, llm) -> Agent:
    goal, backstory = ROLES[role]
    return Agent(role=role, goal=goal, backstory=backstory, llm=llm, allow_delegation=False, max_iter=3,
                 verbose=False)


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


def build_crew(llm, repo_dir: Path, on_task=None) -> Crew:
    planner, reviewer = _agent("Repair Planner", llm), _agent("Code Reviewer", llm)
    plan_guard, review_guard = _guard(_as(PlanDraft)), _guard(lambda o: _as(PlanReview)(o).final_plan)
    plan_guard.repo_dir = review_guard.repo_dir = repo_dir
    plan = Task(name="draft repair plan", description=PLAN_TASK, expected_output="A PlanDraft with complete function sources",
                agent=planner, output_pydantic=PlanDraft, guardrail=plan_guard, guardrail_max_retries=2)
    review = Task(name="review plan", description=REVIEW_TASK, expected_output="A PlanReview with the final plan",
                  agent=reviewer, context=[plan], output_pydantic=PlanReview, guardrail=review_guard,
                  guardrail_max_retries=2)
    return Crew(agents=[planner, reviewer], tasks=[plan, review], process=Process.sequential,
                verbose=False, task_callback=on_task)


def run_planner(evidence: str, investigation: InvestigationReport, root_cause: RootCauseReport,
                repo_dir: Path, llm, records: list[AgentRecord]) -> PlanReview:
    """Run Planner -> Reviewer. `llm` is a model name (e.g. gemini-3.1-flash-lite) or a CrewAI LLM."""
    llm, model_name = _llm(llm)
    marks = [time.monotonic()]

    def on_task(output):
        now = time.monotonic()
        records.append(AgentRecord(agent=str(output.agent).strip() or "crew", framework="crewai", model=model_name,
                                   seconds=round(now - marks[-1], 2), message=f"task done: {output.name or 'task'}"))
        marks.append(now)

    inputs = {"evidence": evidence, "root_cause": root_cause.model_dump_json(),
              "investigation": investigation.failure_summary}
    result = with_retries(lambda: build_crew(llm, repo_dir, on_task).kickoff(inputs=inputs),
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
                records: list[AgentRecord]):
    llm, model_name = _llm(llm)
    agent = _agent(role, llm)
    task = Task(name=name, description=description, expected_output=expected, agent=agent,
                output_pydantic=output_model)
    start = time.monotonic()
    result = with_retries(lambda: Crew(agents=[agent], tasks=[task], process=Process.sequential,
                                       verbose=False).kickoff(inputs=inputs),
                          agent=role, framework="crewai", records=records)
    usage = result.token_usage
    records.append(AgentRecord(agent=role, framework="crewai", model=model_name,
                               seconds=round(time.monotonic() - start, 2),
                               prompt_tokens=(usage.prompt_tokens or 0) if usage else 0,
                               output_tokens=(usage.completion_tokens or 0) if usage else 0,
                               message=f"task done: {name}"))
    return result.pydantic if isinstance(result.pydantic, output_model) else output_model.model_validate_json(result.raw)


def run_plan_task(evidence: str, investigation_summary: str, root_cause_json: str, feedback: list[str] | None,
                  llm, records: list[AgentRecord]) -> PlanDraft:
    """Repair Planner as a single-task crew (blackboard mode). `feedback` = guardrail problems, if any."""
    description = PLAN_TASK + (PLAN_FEEDBACK if feedback else "")
    inputs = {"evidence": evidence, "root_cause": root_cause_json, "investigation": investigation_summary,
              "feedback": "\n".join(f"- {p}" for p in feedback or [])}
    return _run_single("Repair Planner", "draft repair plan", description,
                       "A PlanDraft with complete function sources", PlanDraft, inputs, llm, records)


def run_review_task(evidence: str, draft_json: str, llm, records: list[AgentRecord]) -> PlanReview:
    """Code Reviewer as a single-task crew (blackboard mode): reviews the draft read from the board."""
    return _run_single("Code Reviewer", "review plan", REVIEW_TASK + REVIEW_DRAFT,
                       "A PlanReview with the final plan", PlanReview,
                       {"evidence": evidence, "draft": draft_json}, llm, records)
