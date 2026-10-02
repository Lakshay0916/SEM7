"""Persistence for workflows, stage events and stage artifacts (SQLAlchemy Core).

Works with PostgreSQL (the Docker deployment) and SQLite (local development
and fast tests); the backend is chosen by DATABASE_URL.

* workflows - one row per debugging workflow (current state, attempt counter)
* events    - append-only log of state transitions / agent activity
* artifacts - typed stage outputs (risk, pipeline run, investigation, RAG
              retrieval, root cause, plan, approval, repair, test run, PR),
              stored as validated JSON so the full chain is traceable
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)

from app.config import PROJECT_ROOT, settings
from app.models.schemas import WorkflowState, utcnow
from app.orchestration.state import check_transition

ARTIFACT_KINDS = {
    "risk_assessment", "pipeline_run", "failure_event", "quality_gate", "investigation", "rag_retrieval",
    "root_cause", "repair_plan", "approval", "repair_attempt", "test_run", "reflection",
    "pull_request",
}

metadata = MetaData()

workflows = Table(
    "workflows", metadata,
    Column("id", String(32), primary_key=True),
    Column("scenario", String(64)),
    Column("repo_path", Text, nullable=False),
    Column("commit_sha", String(64)),
    Column("branch", String(255)),
    Column("state", String(32), nullable=False),
    Column("attempt", Integer, nullable=False, default=0),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workflow_id", String(32), ForeignKey("workflows.id"), nullable=False),
    Column("ts", String(40), nullable=False),
    Column("state", String(32)),
    Column("agent", String(64)),
    Column("status", String(32)),
    Column("message", Text),
    Column("duration_ms", Integer),
)

artifacts = Table(
    "artifacts", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("workflow_id", String(32), ForeignKey("workflows.id"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("attempt", Integer, nullable=False, default=0),
    Column("created_at", String(40), nullable=False),
    Column("payload", Text, nullable=False),
)

Index("idx_events_wf", events.c.workflow_id)
Index("idx_artifacts_wf", artifacts.c.workflow_id, artifacts.c.kind)

_ENGINES: dict[str, Engine] = {}


def normalise_url(url: str) -> str:
    """Resolve relative sqlite paths against the project root; pass other URLs through."""
    if url.startswith("sqlite:///"):
        p = Path(url.removeprefix("sqlite:///"))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{p}"
    if url.startswith("postgresql://"):  # default to the psycopg 3 driver
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith(("postgresql+", "sqlite")):
        return url
    raise ValueError(f"unsupported DATABASE_URL scheme: {url.split(':', 1)[0]}")


def _engine(url: str) -> Engine:
    if url not in _ENGINES:
        engine = create_engine(url, pool_pre_ping=True)
        metadata.create_all(engine)
        _ENGINES[url] = engine
    return _ENGINES[url]


def _row(r) -> dict:
    return dict(r._mapping)


class Store:
    def __init__(self, url: str | None = None) -> None:
        self.url = normalise_url(url or settings.database_url)
        self.engine = _engine(self.url)

    @property
    def backend(self) -> str:
        return self.engine.dialect.name  # "postgresql" | "sqlite"

    def ping(self) -> bool:
        with self.engine.connect() as c:
            c.execute(select(1))
        return True

    # -------------------------------------------------------------- workflows

    def create_workflow(self, repo_path: str | Path, scenario: str | None = None,
                        commit_sha: str | None = None, branch: str | None = None) -> str:
        wf_id = f"wf-{uuid.uuid4().hex[:10]}"
        now = utcnow().isoformat()
        with self.engine.begin() as c:
            c.execute(insert(workflows).values(
                id=wf_id, scenario=scenario, repo_path=str(repo_path), commit_sha=commit_sha, branch=branch,
                state=WorkflowState.RECEIVED.value, attempt=0, created_at=now, updated_at=now,
            ))
        self.log_event(wf_id, "orchestrator", "ok", f"Commit {(commit_sha or '?')[:10]} received",
                       state=WorkflowState.RECEIVED)
        return wf_id

    def get_workflow(self, wf_id: str) -> dict:
        with self.engine.connect() as c:
            row = c.execute(select(workflows).where(workflows.c.id == wf_id)).first()
        if row is None:
            raise KeyError(f"unknown workflow {wf_id}")
        return _row(row)

    def list_workflows(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(workflows).order_by(workflows.c.created_at.desc()).limit(limit)).all()
        return [_row(r) for r in rows]

    def state(self, wf_id: str) -> WorkflowState:
        return WorkflowState(self.get_workflow(wf_id)["state"])

    def transition(self, wf_id: str, target: WorkflowState, message: str = "", agent: str = "orchestrator") -> None:
        current = self.state(wf_id)
        check_transition(current, target)
        with self.engine.begin() as c:
            c.execute(update(workflows).where(workflows.c.id == wf_id)
                      .values(state=target.value, updated_at=utcnow().isoformat()))
        self.log_event(wf_id, agent, "ok", message or f"{current.value} -> {target.value}", state=target)

    def set_attempt(self, wf_id: str, attempt: int) -> None:
        with self.engine.begin() as c:
            c.execute(update(workflows).where(workflows.c.id == wf_id)
                      .values(attempt=attempt, updated_at=utcnow().isoformat()))

    # ----------------------------------------------------------------- events

    def log_event(self, wf_id: str, agent: str, status: str, message: str,
                  state: WorkflowState | None = None, duration_ms: int | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(insert(events).values(
                workflow_id=wf_id, ts=utcnow().isoformat(), state=state.value if state else None,
                agent=agent, status=status, message=_redact(message), duration_ms=duration_ms,
            ))

    def events(self, wf_id: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(events).where(events.c.workflow_id == wf_id).order_by(events.c.id)).all()
        return [_row(r) for r in rows]

    # -------------------------------------------------------------- artifacts

    def save_artifact(self, wf_id: str, kind: str, payload: BaseModel | dict[str, Any], attempt: int = 0) -> int:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact kind {kind!r}")
        data = payload.model_dump_json() if isinstance(payload, BaseModel) else json.dumps(payload, default=str)
        with self.engine.begin() as c:
            res = c.execute(insert(artifacts).values(
                workflow_id=wf_id, kind=kind, attempt=attempt, created_at=utcnow().isoformat(),
                payload=_redact(data),
            ))
            return int(res.inserted_primary_key[0])

    def artifacts(self, wf_id: str, kind: str | None = None) -> list[dict]:
        q = select(artifacts).where(artifacts.c.workflow_id == wf_id)
        if kind:
            q = q.where(artifacts.c.kind == kind)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(artifacts.c.id)).all()
        return [{**_row(r), "payload": json.loads(r.payload)} for r in rows]

    def latest(self, wf_id: str, kind: str) -> dict | None:
        items = self.artifacts(wf_id, kind)
        return items[-1]["payload"] if items else None

    def trace(self, wf_id: str) -> dict:
        """Everything recorded for a workflow, in order: the commit -> ... -> PR chain."""
        return {"workflow": self.get_workflow(wf_id), "events": self.events(wf_id),
                "artifacts": self.artifacts(wf_id)}


def _redact(text: str) -> str:
    for secret in settings.secret_values():
        text = text.replace(secret, "[REDACTED]")
    return text
