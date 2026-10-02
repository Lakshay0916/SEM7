"""Backend API tests (in-process via FastAPI's TestClient)."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.api.main as api
import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    api.get_store.cache_clear()
    yield TestClient(api.app)
    api.get_store.cache_clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["database"] == "sqlite"


def test_scenarios(client):
    ids = [s["id"] for s in client.get("/scenarios").json()]
    assert {"simple_bug", "high_risk_change", "failed_first_repair"} <= set(ids)


def test_full_self_healing_flow_over_http(client):
    r = client.post("/workflows", json={"scenario": "failed_first_repair"})
    assert r.status_code == 201
    wf = r.json()["workflow_id"]
    assert r.json()["state"] == "WAITING_APPROVAL"  # fix proposed, nothing applied

    states = []
    while client.get(f"/workflows/{wf}").json()["workflow"]["state"] == "WAITING_APPROVAL":
        states.append(client.post(f"/workflows/{wf}/approve", json={"reviewer": "alice"}).json()["state"])
    assert states == ["WAITING_APPROVAL", "PR_CREATED"]  # attempt 1 → reflect → new plan; attempt 2 → PR

    trace = client.get(f"/workflows/{wf}").json()
    kinds = [a["kind"] for a in trace["artifacts"]]
    assert kinds.count("repair_attempt") == 2 and "pull_request" in kinds
    approvals = [a["payload"]["reviewer"] for a in trace["artifacts"] if a["kind"] == "approval"]
    assert approvals == ["alice", "alice"]


def test_reject_then_abort(client):
    wf = client.post("/workflows", json={"scenario": "simple_bug"}).json()["workflow_id"]
    assert client.post(f"/workflows/{wf}/reject", json={"reviewer": "bob", "comment": "no"}).json()["state"] == "REJECTED"
    assert client.post(f"/workflows/{wf}/abort", json={}).json()["state"] == "ABORTED"


def test_approve_without_pending_plan_is_conflict(client):
    wf = client.post("/workflows", json={"scenario": "simple_bug", "heal": False}).json()["workflow_id"]
    r = client.post(f"/workflows/{wf}/approve", json={})
    assert r.status_code == 409  # FAILED, not WAITING_APPROVAL: no repair without a pending approved plan
    assert client.post(f"/workflows/{wf}/propose").json()["state"] == "WAITING_APPROVAL"


def test_errors(client):
    assert client.get("/workflows/wf-missing").status_code == 404
    assert client.post("/workflows", json={"scenario": "nope"}).status_code == 404
    assert client.post("/workflows/wf-missing/approve", json={"reviewer": ""}).status_code == 422


def test_safety_check_endpoint(client):
    wf = client.post("/workflows", json={"scenario": "simple_bug", "heal": False}).json()["workflow_id"]
    r = client.post(f"/workflows/{wf}/safety-check").json()
    assert r["blocked"] is True and r["main_unchanged"] is True


def test_risk_endpoints(client):
    assert "variants" in client.get("/risk/model").json()
    ev = client.get("/risk/evaluation").json()
    assert ev["evaluation"]["dataset"]["rows"] > 0
