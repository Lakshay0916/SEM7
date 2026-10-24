"""A2A service hosting the Google ADK agents (Investigator, Root-cause analyst).

    python -m app.a2a_agents.adk_service          # http://localhost:8101, card at /.well-known/agent-card.json

Skills:
    investigator        evidence -> InvestigationReport
    root_cause_analyst  evidence + investigation -> RootCauseReport
    analyse_and_plan    direct hand-off: runs both ADK agents, then sends the root cause to the CrewAI
                        planning service over A2A and returns its reviewed plan (ADK -> CrewAI via A2A)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.a2a_agents.protocol import agent_card, build_app, call_skill, public_url
from app.agents.models import AgentRecord
from app.config import settings
from app.mcp_servers.tools import RepairScope

PORT = int(os.getenv("A2A_PORT", "8101"))
CREW_URL = os.getenv("CREW_AGENT_URL", "http://localhost:8102/")


def _scope(payload: dict) -> RepairScope | None:
    s = payload.get("scope")
    return RepairScope(Path(s["repo_dir"]), s["base_sha"], s["head_sha"]) if s else None


def _out(result, records: list[AgentRecord]) -> dict:
    return {"result": result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
            "records": [r.model_dump() for r in records]}


def investigator(payload: dict) -> dict:
    from app.agents.adk_analysts import run_agent

    records: list[AgentRecord] = []
    inv = run_agent("investigator", {"evidence": payload["evidence"]}, payload.get("model") or settings.agent_model,
                    records, _scope(payload))
    return _out(inv, records)


def root_cause_analyst(payload: dict) -> dict:
    from app.agents.adk_analysts import run_agent

    records: list[AgentRecord] = []
    rc = run_agent("root_cause_analyst", {"evidence": payload["evidence"],
                                          "investigation": json.dumps(payload["investigation"])},
                   payload.get("model") or settings.agent_model, records, _scope(payload))
    return _out(rc, records)


def analyse_and_plan(payload: dict) -> dict:
    from app.agents.adk_analysts import run_analysts

    records: list[AgentRecord] = []
    inv, rc = run_analysts(payload["evidence"], payload.get("model") or settings.agent_model, records,
                           _scope(payload))
    # Hand-off to the CrewAI agents: an A2A call from this (ADK) agent service to the CrewAI service.
    start = time.monotonic()
    crew = call_skill(payload.get("crew_url") or CREW_URL, "plan_and_review",
                      {**payload, "investigation": inv.model_dump(mode="json"), "root_cause": rc.model_dump(mode="json")})
    records.append(AgentRecord(agent="root_cause_analyst → sem7-crewai/plan_and_review", framework="a2a",
                               seconds=round(time.monotonic() - start, 2),
                               message="ADK agent handed the root cause to the CrewAI crew over A2A"))
    records += [AgentRecord.model_validate(r) for r in crew["records"]]
    return {"result": {"investigation": inv.model_dump(mode="json"), "root_cause": rc.model_dump(mode="json"),
                       "review": crew["result"]},
            "records": [r.model_dump() for r in records]}


SKILLS = {"investigator": investigator, "root_cause_analyst": root_cause_analyst, "analyse_and_plan": analyse_and_plan}
CARD = agent_card(
    "sem7-adk-analysts", "Google ADK agents that investigate a CI failure and find its root cause.",
    [("investigator", "Investigator", "What failed: facts vs hypotheses, from the evidence package"),
     ("root_cause_analyst", "Root-cause analyst", "Why it failed, with confidence and alternatives"),
     ("analyse_and_plan", "Analyse and plan (direct hand-off)",
      "Both ADK agents, then hands the root cause to the CrewAI planning agents over A2A")],
    public_url(PORT))
app = build_app(CARD, SKILLS)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("A2A_HOST", "0.0.0.0"), port=PORT)
