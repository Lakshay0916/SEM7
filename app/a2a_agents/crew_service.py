"""A2A service hosting the CrewAI agents (Repair Planner, Code Reviewer).

    python -m app.a2a_agents.crew_service         # http://localhost:8102, card at /.well-known/agent-card.json

Skills:
    plan             evidence + root cause (+ guardrail feedback) -> PlanDraft
    review           evidence + draft -> PlanReview
    plan_and_review  direct hand-off target: the sequential crew (planner -> reviewer with task context)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.a2a_agents.protocol import agent_card, build_app, public_url
from app.agents.models import AgentRecord
from app.config import settings
from app.mcp_servers.tools import RepairScope
from app.models.schemas import InvestigationReport, RootCauseReport

PORT = int(os.getenv("A2A_PORT", "8102"))


def _scope(payload: dict) -> RepairScope | None:
    s = payload.get("scope")
    return RepairScope(Path(s["repo_dir"]), s["base_sha"], s["head_sha"]) if s else None


def _out(result, records: list[AgentRecord]) -> dict:
    return {"result": result.model_dump(mode="json"), "records": [r.model_dump() for r in records]}


def plan(payload: dict) -> dict:
    from app.agents.crew_planner import run_plan_task

    records: list[AgentRecord] = []
    draft = run_plan_task(payload["evidence"], payload["investigation_summary"], payload["root_cause"],
                          payload.get("feedback"), payload.get("model") or settings.agent_model, records,
                          _scope(payload))
    return _out(draft, records)


def review(payload: dict) -> dict:
    from app.agents.crew_planner import run_review_task

    records: list[AgentRecord] = []
    result = run_review_task(payload["evidence"], payload["draft"], payload.get("model") or settings.agent_model,
                             records, _scope(payload))
    return _out(result, records)


def plan_and_review(payload: dict) -> dict:
    from app.agents.crew_planner import run_planner

    records: list[AgentRecord] = []
    result = run_planner(payload["evidence"], InvestigationReport.model_validate(payload["investigation"]),
                         RootCauseReport.model_validate(payload["root_cause"]), Path(payload["repo_dir"]),
                         payload.get("model") or settings.agent_model, records, _scope(payload))
    return _out(result, records)


SKILLS = {"plan": plan, "review": review, "plan_and_review": plan_and_review}
CARD = agent_card(
    "sem7-crewai-planning", "CrewAI agents that plan a minimal fix and review it.",
    [("plan", "Repair Planner", "Smallest source-code change that makes the failing tests pass"),
     ("review", "Code Reviewer", "Approves the draft or returns a corrected plan"),
     ("plan_and_review", "Plan and review (crew)", "Planner -> reviewer as one sequential crew")],
    public_url(PORT))
app = build_app(CARD, SKILLS)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("A2A_HOST", "0.0.0.0"), port=PORT)
