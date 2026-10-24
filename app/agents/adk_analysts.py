"""Google ADK analysts: Investigator -> Root-cause analyst (an ADK SequentialAgent).

Both agents read the evidence package from ADK session state and write a typed report back
to state (`output_key`), which the next agent reads - state is the hand-off between them.
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


def build_analysts(model) -> SequentialAgent:
    cfg = types.GenerateContentConfig(temperature=0)
    investigator = LlmAgent(
        name="investigator", model=model, description="Finds what failed, from evidence only",
        instruction=INVESTIGATOR, output_schema=InvestigationReport, output_key="investigation",
        generate_content_config=cfg,
    )
    root_cause = LlmAgent(
        name="root_cause_analyst", model=model, description="Explains why the tests fail",
        instruction=ROOT_CAUSE, output_schema=RootCauseReport, output_key="root_cause",
        generate_content_config=cfg,
    )
    return SequentialAgent(name="adk_analysts", sub_agents=[investigator, root_cause])


async def _run(evidence: str, model, model_name: str) -> tuple[dict, list[AgentRecord]]:
    runner = InMemoryRunner(agent=build_analysts(model), app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id="pipeline",
                                                          state={"evidence": evidence})
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
                           output_tokens=tokens[a][1], message="report written to session state")
               for a in ("investigator", "root_cause_analyst") if a in started]
    # Each agent's time runs from the previous agent's last event to its own last event.
    t0 = run_start
    for r in records:
        r.seconds = round(ended[r.agent] - t0, 2)
        t0 = ended[r.agent]
    return state, records


def run_analysts(evidence: str, model, records: list[AgentRecord]) -> tuple[InvestigationReport, RootCauseReport]:
    """Run Investigator -> Root-cause on the evidence. `model` is a model name or an ADK BaseLlm."""
    model_name = model if isinstance(model, str) else type(model).__name__
    state, recs = with_retries(lambda: asyncio.run(_run(evidence, model, model_name)),
                               agent="adk_analysts", framework="adk", records=records)
    records.extend(recs)
    return (InvestigationReport.model_validate(state["investigation"]),
            RootCauseReport.model_validate(state["root_cause"]))
