"""Google ADK analysts: Investigator and Root-cause analyst.

Two investigation patterns (see AGENT_PATTERN):
* sequential - one Investigator, then the Root-cause analyst.
* parallel   - three specialist investigators run at the same time in an ADK ParallelAgent (failing
               tests, code change, risk and history), then an Investigation Lead merges their findings
               into the report the Root-cause analyst reads (fan-out / fan-in).

Two ways to run them (see AGENT_MESSAGING):
* direct     - one ADK SequentialAgent; the investigator writes its report to ADK session state
               (`output_key`) and the root-cause agent reads it from there (point-to-point hand-off).
* blackboard - each agent runs on its own (`run_agent`) with inputs taken from the shared blackboard,
               and its report is posted back to the board by the controller.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agents.mcp_tools import adk_tool_callbacks, adk_toolsets, hint
from app.agents.models import AgentRecord, SpecialistFinding
from app.agents.retry import with_retries
from app.models.schemas import InvestigationReport, RootCauseReport

APP = "sem7-adk"

INVESTIGATOR = """You are the Investigation Agent of a CI self-healing system.
Read the evidence below and report what failed. Rules:
- Only state facts that appear in the evidence (test names, error messages, files, functions in the diff).
- Put every fact in `evidence` prefixed with "FACT:", and every guess in `initial_hypotheses` prefixed with "HYPOTHESIS:".
- `affected_files` lists only source files (never tests).

EVIDENCE:
{evidence}"""

ROOT_CAUSE = """You are the Root Cause Agent of a CI self-healing system.
Using the evidence and the investigator's report, explain WHY the tests fail. Rules:
- Point to the exact function and line(s) in the developer's diff that cause each failure; compare with the
  baseline version of the function when it is shown.
- If earlier repair attempts are listed, explain why they were not enough and what is still wrong.
- `confidence` is between 0 and 1 and reflects how directly the evidence supports your conclusion.
- List other plausible causes in `alternative_hypotheses`. Never invent code that is not in the evidence.

INVESTIGATOR REPORT:
{investigation}

EVIDENCE:
{evidence}"""


_SPECIALIST = """You are the {title}, one of three investigators examining the same CI failure in parallel.
{angle}
Rules:
- Only state facts that appear in the evidence or in your tool results; prefix each with "FACT:".
- Prefix every guess with "HYPOTHESIS:".
- `suspect_functions` lists source functions (never tests) as file::function.
- Set `focus` to "{focus}".

EVIDENCE:
{{evidence}}"""

TEST_ANALYST = _SPECIALIST.format(
    title="Failing-test Analyst", focus="failing tests",
    angle="Look only at the failing tests: their names, assertion messages, tracebacks and test source. "
          "Say what each test expected and what it got, and which behaviour of the code under test must be wrong.")
CHANGE_ANALYST = _SPECIALIST.format(
    title="Code-change Analyst", focus="code change",
    angle="Look only at the developer's change: the diff and the baseline version of each changed function. "
          "Say exactly what changed in each function and which change could break the tests.")
HISTORY_ANALYST = _SPECIALIST.format(
    title="Risk and History Analyst", focus="risk and history",
    angle="Look at how risky and how familiar this change is: its size, the files it touches, earlier repair "
          "attempts, and (with tools) the ML risk score and similar past failures.")

INVESTIGATION_LEAD = """You are the Investigation Lead of a CI self-healing system.
Three specialists examined the same failure in parallel. Merge their findings into one investigation report.
Rules:
- Keep only facts that appear in the evidence; drop anything a specialist claims that the evidence contradicts.
- Put facts in `evidence` prefixed with "FACT:" and guesses in `initial_hypotheses` prefixed with "HYPOTHESIS:".
- Where specialists agree, say so; `affected_files` lists only source files (never tests).

FAILING-TEST ANALYST:
{finding_tests}

CODE-CHANGE ANALYST:
{finding_change}

RISK AND HISTORY ANALYST:
{finding_history}

EVIDENCE:
{evidence}"""


# name -> (description, instruction, output schema, state key it writes)
SPECS = {
    "investigator": ("Finds what failed, from evidence only", INVESTIGATOR, InvestigationReport, "investigation"),
    "test_analyst": ("Investigates the failing tests", TEST_ANALYST, SpecialistFinding, "finding_tests"),
    "change_analyst": ("Investigates the developer's change", CHANGE_ANALYST, SpecialistFinding, "finding_change"),
    "history_analyst": ("Investigates risk and past failures", HISTORY_ANALYST, SpecialistFinding, "finding_history"),
    "investigation_lead": ("Merges the specialists' findings", INVESTIGATION_LEAD, InvestigationReport,
                           "investigation"),
    "root_cause_analyst": ("Explains why the tests fail", ROOT_CAUSE, RootCauseReport, "root_cause"),
}
SPECIALISTS = ["test_analyst", "change_analyst", "history_analyst"]
PATTERNS = ("sequential", "parallel")  # the hierarchical pattern lives in the CrewAI planning half


def build_agent(name: str, model, scope=None, records: list[AgentRecord] | None = None) -> LlmAgent:
    """One ADK agent; with a repair `scope` it gets its MCP tools and tool-call monitoring callbacks."""
    description, instruction, schema, key = SPECS[name]
    before, after = adk_tool_callbacks(name, records if records is not None else [])
    return LlmAgent(name=name, model=model, description=description, instruction=instruction + hint(name, scope),
                    output_schema=schema, output_key=key, tools=adk_toolsets(name, scope),
                    before_tool_callback=before, after_tool_callback=after,
                    generate_content_config=types.GenerateContentConfig(temperature=0))


def build_parallel_investigation(model, scope=None, records=None) -> SequentialAgent:
    """Fan-out / fan-in: three specialists at once (ParallelAgent), then the lead merges their findings."""
    return SequentialAgent(name="parallel_investigation", sub_agents=[
        ParallelAgent(name="specialists", sub_agents=[build_agent(n, model, scope, records) for n in SPECIALISTS]),
        build_agent("investigation_lead", model, scope, records)])


def build_analysts(model, scope=None, records=None, pattern: str = "sequential") -> SequentialAgent:
    first = (build_parallel_investigation(model, scope, records) if pattern == "parallel"
             else build_agent("investigator", model, scope, records))
    return SequentialAgent(name="adk_analysts", sub_agents=[first, build_agent("root_cause_analyst", model, scope,
                                                                               records)])


def stages(pattern: str, root_cause: bool = True) -> list[list[str]]:
    """The order agents run in; agents in one stage run at the same time."""
    out = [SPECIALISTS, ["investigation_lead"]] if pattern == "parallel" else [["investigator"]]
    return out + ([["root_cause_analyst"]] if root_cause else [])


async def _run(agent, state: dict, model_name: str, order: list[list[str]]) -> tuple[dict, list[AgentRecord]]:
    runner = InMemoryRunner(agent=agent, app_name=APP)
    try:
        return await _drive(runner, state, model_name, order)
    finally:
        await runner.close()  # stops the MCP server subprocesses


async def _drive(runner, state: dict, model_name: str, order: list[list[str]]) -> tuple[dict, list[AgentRecord]]:
    session = await runner.session_service.create_session(app_name=APP, user_id="pipeline", state=state)
    run_start = time.monotonic()
    started: dict[str, float] = {}
    ended: dict[str, float] = {}
    tokens: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    message = types.Content(role="user", parts=[types.Part(text="Analyse the evidence in your instructions.")])
    async for event in runner.run_async(user_id="pipeline", session_id=session.id, new_message=message):
        now = time.monotonic()
        author = event.author or "unknown"
        started.setdefault(author, now)
        ended[author] = now
        if event.usage_metadata:
            tokens[author][0] += event.usage_metadata.prompt_token_count or 0
            tokens[author][1] += event.usage_metadata.candidates_token_count or 0
    state = (await runner.session_service.get_session(app_name=APP, user_id="pipeline",
                                                      session_id=session.id)).state
    # Each agent's time runs from the end of the previous stage to its own last event; agents in one
    # (parallel) stage share the same start, so their times overlap instead of adding up.
    records, t0 = [], run_start
    for stage in order:
        ran = [a for a in stage if a in started]
        for a in ran:
            records.append(AgentRecord(agent=a, framework="adk", model=model_name, seconds=round(ended[a] - t0, 2),
                                       prompt_tokens=tokens[a][0], output_tokens=tokens[a][1],
                                       message=f"wrote `{SPECS[a][3]}`" + (" (parallel branch)" if len(stage) > 1
                                                                           else "")))
        if ran:
            t0 = max(ended[a] for a in ran)
    return state, records


def run_analysts(evidence: str, model, records: list[AgentRecord], scope=None,
                 pattern: str = "sequential") -> tuple[InvestigationReport, RootCauseReport]:
    """Run the investigation (one agent, or three in parallel + lead) -> Root-cause on the evidence.

    `model` is a model name or an ADK BaseLlm."""
    model_name = _name(model)
    state, recs = with_retries(lambda: asyncio.run(_run(build_analysts(model, scope, records, pattern),
                                                        {"evidence": evidence}, model_name, stages(pattern))),
                               agent="adk_analysts", framework="adk", records=records)
    records.extend(recs)
    return (InvestigationReport.model_validate(state["investigation"]),
            RootCauseReport.model_validate(state["root_cause"]))


def run_agent(name: str, inputs: dict, model, records: list[AgentRecord], scope=None):
    """Run one ADK agent on `inputs` (its instruction placeholders) and return its typed report."""
    _, _, schema, key = SPECS[name]
    state, recs = with_retries(lambda: asyncio.run(_run(build_agent(name, model, scope, records), dict(inputs),
                                                        _name(model), [[name]])),
                               agent=name, framework="adk", records=records)
    records.extend(recs)
    return schema.model_validate(state[key])


def run_parallel_investigation(evidence: str, model, records: list[AgentRecord],
                               scope=None) -> tuple[InvestigationReport, dict[str, SpecialistFinding]]:
    """The parallel investigation on its own (blackboard mode): the merged report + each specialist's finding."""
    state, recs = with_retries(lambda: asyncio.run(_run(build_parallel_investigation(model, scope, records),
                                                        {"evidence": evidence}, _name(model),
                                                        stages("parallel", root_cause=False))),
                               agent="parallel_investigation", framework="adk", records=records)
    records.extend(recs)
    findings = {n: SpecialistFinding.model_validate(state[SPECS[n][3]]) for n in SPECIALISTS}
    return InvestigationReport.model_validate(state["investigation"]), findings


def _name(model) -> str:
    return model if isinstance(model, str) else type(model).__name__
