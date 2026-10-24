"""The orchestrator's side of A2A: agents that live in other services, called over the protocol.

AGENT_TRANSPORT=a2a makes the strategy use these instead of in-process agents:
* blackboard - each knowledge source sends its inputs (read from the board) to the right service's skill;
* direct     - one call to the ADK service, which hands off to the CrewAI service itself over A2A.
Every A2A call is recorded (caller → service/skill, time); the remote agents' own records come back
with the result and are merged, so monitoring looks the same as in-process.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.agents.blackboard import KnowledgeSource
from app.agents.models import AgentRecord, PlanDraft, PlanReview
from app.config import settings
from app.mcp_servers.tools import RepairScope
from app.models.schemas import InvestigationReport, RootCauseReport


def _scope_json(scope: RepairScope | None) -> dict | None:
    return {"repo_dir": str(scope.repo_dir), "base_sha": scope.base_sha, "head_sha": scope.head_sha} if scope else None


def _call(url: str, service: str, skill: str, payload: dict, records: list[AgentRecord], caller: str) -> dict:
    from app.a2a_agents.protocol import call_skill

    start = time.monotonic()
    out = call_skill(url, skill, payload)
    records.append(AgentRecord(agent=f"{caller} → {service}/{skill}", framework="a2a",
                               seconds=round(time.monotonic() - start, 2), message=f"A2A message/send to {url}"))
    records.extend(AgentRecord.model_validate(r) for r in out.get("records", []))
    return out["result"]


def remote_sources(scope: RepairScope | None, adk_url: str | None = None,
                   crew_url: str | None = None) -> list[KnowledgeSource]:
    adk, crew = adk_url or settings.adk_agent_url, crew_url or settings.crew_agent_url
    base = {"scope": _scope_json(scope), "model": settings.agent_model}

    def investigator(bb, records):
        inv = InvestigationReport.model_validate(
            _call(adk, "sem7-adk", "investigator", {**base, "evidence": bb.get("evidence")}, records, "blackboard"))
        return inv, inv.failure_summary

    def root_cause_analyst(bb, records):
        rc = RootCauseReport.model_validate(_call(adk, "sem7-adk", "root_cause_analyst", {
            **base, "evidence": bb.get("evidence"), "investigation": bb.get("investigation")}, records, "blackboard"))
        return rc, f"{rc.root_cause} (confidence {rc.confidence:.0%})"

    def planner(bb, records):
        draft = PlanDraft.model_validate(_call(crew, "sem7-crewai", "plan", {
            **base, "evidence": bb.get("evidence"), "investigation_summary": bb.get("investigation")["failure_summary"],
            "root_cause": json.dumps(bb.get("root_cause")), "feedback": bb.get("feedback:plan_draft")},
            records, "blackboard"))
        return draft, draft.summary

    def reviewer(bb, records):
        review = PlanReview.model_validate(_call(crew, "sem7-crewai", "review", {
            **base, "evidence": bb.get("evidence"), "draft": json.dumps(bb.get("plan_draft"))}, records, "blackboard"))
        verdict = "approved" if review.approved else "corrected: " + "; ".join(review.issues)
        return review, f"{verdict} - {review.final_plan.summary}"

    return [
        KnowledgeSource("investigator", "adk", ("evidence",), "investigation", investigator),
        KnowledgeSource("root_cause_analyst", "adk", ("evidence", "investigation"), "root_cause", root_cause_analyst),
        KnowledgeSource("Repair Planner", "crewai", ("evidence", "root_cause"), "plan_draft", planner),
        KnowledgeSource("Code Reviewer", "crewai", ("evidence", "root_cause", "plan_draft"), "review", reviewer),
    ]


class RemoteDirect:
    """Direct hand-off over A2A: orchestrator → ADK service → (A2A) → CrewAI service."""

    def __init__(self, adk_url: str | None = None, crew_url: str | None = None) -> None:
        self.adk, self.crew = adk_url or settings.adk_agent_url, crew_url or settings.crew_agent_url
        self.reviews: dict[str, PlanReview] = {}

    def analyse(self, evidence: str, records: list[AgentRecord], scope: RepairScope | None, repo_dir: Path):
        out = _call(self.adk, "sem7-adk", "analyse_and_plan",
                    {"evidence": evidence, "scope": _scope_json(scope), "model": settings.agent_model,
                     "repo_dir": str(repo_dir), "crew_url": self.crew}, records, "orchestrator")
        self.reviews[str(repo_dir)] = PlanReview.model_validate(out["review"])
        return InvestigationReport.model_validate(out["investigation"]), RootCauseReport.model_validate(out["root_cause"])

    def plan(self, repo_dir: Path) -> PlanDraft:
        return self.reviews.pop(str(repo_dir)).final_plan
