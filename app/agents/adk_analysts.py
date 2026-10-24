"""Google ADK analysts: Investigator and Root-cause analyst.

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

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agents.models import AgentRecord
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


# name -> (description, instruction, output schema, state key it writes)
SPECS = {
    "investigator": ("Finds what failed, from evidence only", INVESTIGATOR, InvestigationReport, "investigation"),
    "root_cause_analyst": ("Explains why the tests fail", ROOT_CAUSE, RootCauseReport, "root_cause"),
}


def build_agent(name: str, model) -> LlmAgent:
    description, instruction, schema, key = SPECS[name]
    return LlmAgent(name=name, model=model, description=description, instruction=instruction,
                    output_schema=schema, output_key=key,
                    generate_content_config=types.GenerateContentConfig(temperature=0))


def build_analysts(model) -> SequentialAgent:
    return SequentialAgent(name="adk_analysts",
                           sub_agents=[build_agent("investigator", model), build_agent("root_cause_analyst", model)])


async def _run(agent, state: dict, model_name: str) -> tuple[dict, list[AgentRecord]]:
    runner = InMemoryRunner(agent=agent, app_name=APP)
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
    records = [AgentRecord(agent=a, framework="adk", model=model_name, prompt_tokens=tokens[a][0],
                           output_tokens=tokens[a][1], message=f"wrote `{SPECS[a][3]}`")
               for a in SPECS if a in started]
    # Each agent's time runs from the previous agent's last event to its own last event.
    t0 = run_start
    for r in records:
        r.seconds = round(ended[r.agent] - t0, 2)
        t0 = ended[r.agent]
    return state, records


def run_analysts(evidence: str, model, records: list[AgentRecord]) -> tuple[InvestigationReport, RootCauseReport]:
    """Run Investigator -> Root-cause on the evidence. `model` is a model name or an ADK BaseLlm."""
    model_name = _name(model)
    state, recs = with_retries(lambda: asyncio.run(_run(build_analysts(model), {"evidence": evidence}, model_name)),
                               agent="adk_analysts", framework="adk", records=records)
    records.extend(recs)
    return (InvestigationReport.model_validate(state["investigation"]),
            RootCauseReport.model_validate(state["root_cause"]))


def run_agent(name: str, inputs: dict, model, records: list[AgentRecord]):
    """Run one ADK agent on `inputs` (its instruction placeholders) and return its typed report."""
    _, _, schema, key = SPECS[name]
    state, recs = with_retries(lambda: asyncio.run(_run(build_agent(name, model), dict(inputs), _name(model))),
                               agent=name, framework="adk", records=records)
    records.extend(recs)
    return schema.model_validate(state[key])


def _name(model) -> str:
    return model if isinstance(model, str) else type(model).__name__
