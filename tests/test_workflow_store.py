import logging

import pytest

from app.config import settings
from app.logging_utils import SecretRedactingFilter
from app.models.schemas import RiskAssessment, RiskLevel
from app.models.schemas import WorkflowState as S
from app.orchestration.state import InvalidTransition, check_transition
from app.orchestration.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'test.sqlite3'}")


HAPPY_PATH = [S.RISK_ANALYZED, S.TESTING, S.FAILED, S.INVESTIGATING, S.ROOT_CAUSE_IDENTIFIED,
              S.SOLUTION_PROPOSED, S.WAITING_APPROVAL, S.APPROVED, S.REPAIRING, S.TESTING_REPAIR,
              S.PASSED, S.PR_CREATED]


def test_happy_path_is_legal(store):
    wf = store.create_workflow("/tmp/repo", "simple_bug", "abc123", "feature/x")
    for s in HAPPY_PATH:
        store.transition(wf, s)
    assert store.state(wf) == S.PR_CREATED
    assert [e["state"] for e in store.events(wf)] == ["RECEIVED"] + [s.value for s in HAPPY_PATH]


def test_reflection_loop_is_legal(store):
    wf = store.create_workflow("/tmp/repo")
    for s in HAPPY_PATH[:10] + [S.REFLECTING, S.INVESTIGATING]:
        store.transition(wf, s)
    assert store.state(wf) == S.INVESTIGATING


@pytest.mark.parametrize("current,target", [
    (S.WAITING_APPROVAL, S.REPAIRING),   # no repair without approval
    (S.SOLUTION_PROPOSED, S.APPROVED),   # no approval without waiting for a human
    (S.TESTING_REPAIR, S.PR_CREATED),    # no PR without PASSED
    (S.FAILED, S.PR_CREATED),
    (S.REJECTED, S.REPAIRING),
])
def test_illegal_transitions(current, target):
    with pytest.raises(InvalidTransition):
        check_transition(current, target)


def test_terminal_states_are_final():
    with pytest.raises(InvalidTransition):
        check_transition(S.PR_CREATED, S.ABORTED)
    check_transition(S.REPAIRING, S.ABORTED)  # abort allowed from non-terminal


def test_artifacts_round_trip_and_trace(store):
    wf = store.create_workflow("/tmp/repo")
    ra = RiskAssessment(commit_sha="abc", risk_score=0.8, risk_level=RiskLevel.HIGH, model_version="v1")
    store.save_artifact(wf, "risk_assessment", ra)
    store.save_artifact(wf, "approval", {"decision": "approve", "by": "reviewer"}, attempt=1)
    assert store.latest(wf, "risk_assessment")["risk_level"] == "HIGH"
    trace = store.trace(wf)
    assert [a["kind"] for a in trace["artifacts"]] == ["risk_assessment", "approval"]
    with pytest.raises(ValueError):
        store.save_artifact(wf, "made_up_kind", {})


def test_secrets_redacted_in_store(store, monkeypatch):
    monkeypatch.setattr(type(settings), "secret_values", lambda self: ["sk-live-123"])
    wf = store.create_workflow("/tmp/repo")
    store.log_event(wf, "agent", "ok", "calling API with sk-live-123")
    store.save_artifact(wf, "investigation", {"note": "token sk-live-123"})
    assert "sk-live-123" not in str(store.trace(wf))


def test_log_filter_redacts():
    rec = logging.LogRecord("x", logging.INFO, "", 0, "auth %s", ("ghp_secret",), None)
    SecretRedactingFilter(["ghp_secret"]).filter(rec)
    assert rec.getMessage() == "auth [REDACTED]"


def test_run_ci_workflow_records_trace(store, tmp_path):
    from app.orchestration.pipeline import run_ci_workflow

    res = run_ci_workflow("simple_bug", store, predictor=None, workspace=tmp_path)
    assert store.state(res.workflow_id) == S.FAILED
    kinds = [a["kind"] for a in store.artifacts(res.workflow_id)]
    assert kinds == ["pipeline_run", "failure_event"]
    states = [e["state"] for e in store.events(res.workflow_id) if e["state"]]
    assert states == ["RECEIVED", "TESTING", "FAILED"]

    ok = run_ci_workflow("high_risk_change", store, predictor=None, workspace=tmp_path)
    assert store.state(ok.workflow_id) == S.PASSED
