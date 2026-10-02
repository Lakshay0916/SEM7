"""SQLite persistence for workflows, stage events and stage artifacts.

* workflows - one row per debugging workflow (current state, attempt counter)
* events    - append-only log of state transitions / agent activity
* artifacts - typed stage outputs (risk, pipeline run, investigation, RAG
              retrieval, root cause, plan, approval, repair, test run, PR),
              stored as validated JSON so the full chain is traceable
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel

from app.config import PROJECT_ROOT, settings
from app.models.schemas import WorkflowState, utcnow
from app.orchestration.state import check_transition

ARTIFACT_KINDS = {
    "risk_assessment", "pipeline_run", "failure_event", "investigation", "rag_retrieval",
    "root_cause", "repair_plan", "approval", "repair_attempt", "test_run", "reflection",
    "pull_request",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    scenario TEXT,
    repo_path TEXT NOT NULL,
    commit_sha TEXT,
    branch TEXT,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    ts TEXT NOT NULL,
    state TEXT,
    agent TEXT,
    status TEXT,
    message TEXT,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    kind TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_wf ON events(workflow_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_wf ON artifacts(workflow_id, kind);
"""


def _db_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise ValueError(f"only sqlite:/// URLs are supported, got {url!r}")
    p = Path(url.removeprefix("sqlite:///"))
    return p if p.is_absolute() else PROJECT_ROOT / p


class Store:
    def __init__(self, url: str | None = None) -> None:
        self.path = _db_path(url or settings.database_url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -------------------------------------------------------------- workflows

    def create_workflow(self, repo_path: str | Path, scenario: str | None = None,
                        commit_sha: str | None = None, branch: str | None = None) -> str:
        wf_id = f"wf-{uuid.uuid4().hex[:10]}"
        now = utcnow().isoformat()
        with self._conn() as c:
            c.execute(
                "INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?)",
                (wf_id, scenario, str(repo_path), commit_sha, branch,
                 WorkflowState.RECEIVED.value, 0, now, now),
            )
        self.log_event(wf_id, "orchestrator", "ok", f"Commit {(commit_sha or '?')[:10]} received",
                       state=WorkflowState.RECEIVED)
        return wf_id

    def get_workflow(self, wf_id: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown workflow {wf_id}")
        return dict(row)

    def list_workflows(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM workflows ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def state(self, wf_id: str) -> WorkflowState:
        return WorkflowState(self.get_workflow(wf_id)["state"])

    def transition(self, wf_id: str, target: WorkflowState, message: str = "", agent: str = "orchestrator") -> None:
        current = self.state(wf_id)
        check_transition(current, target)
        with self._conn() as c:
            c.execute("UPDATE workflows SET state=?, updated_at=? WHERE id=?",
                      (target.value, utcnow().isoformat(), wf_id))
        self.log_event(wf_id, agent, "ok", message or f"{current.value} -> {target.value}", state=target)

    def set_attempt(self, wf_id: str, attempt: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE workflows SET attempt=?, updated_at=? WHERE id=?",
                      (attempt, utcnow().isoformat(), wf_id))

    # ----------------------------------------------------------------- events

    def log_event(self, wf_id: str, agent: str, status: str, message: str,
                  state: WorkflowState | None = None, duration_ms: int | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events (workflow_id, ts, state, agent, status, message, duration_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                (wf_id, utcnow().isoformat(), state.value if state else None, agent, status,
                 _redact(message), duration_ms),
            )

    def events(self, wf_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE workflow_id=? ORDER BY id", (wf_id,)).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- artifacts

    def save_artifact(self, wf_id: str, kind: str, payload: BaseModel | dict[str, Any], attempt: int = 0) -> int:
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact kind {kind!r}")
        data = payload.model_dump_json() if isinstance(payload, BaseModel) else json.dumps(payload, default=str)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO artifacts (workflow_id, kind, attempt, created_at, payload) VALUES (?,?,?,?,?)",
                (wf_id, kind, attempt, utcnow().isoformat(), _redact(data)),
            )
            return int(cur.lastrowid)

    def artifacts(self, wf_id: str, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM artifacts WHERE workflow_id=?", [wf_id]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

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
