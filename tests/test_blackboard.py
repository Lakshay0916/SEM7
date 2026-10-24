"""Blackboard architecture: board semantics, controller scheduling, guard feedback, persistence via the loop."""

from dataclasses import replace

import pytest

import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod
from app.agents.blackboard import Blackboard, BlackboardController, BlackboardStuck, Guard, KnowledgeSource
from app.agents.models import AgentRecord, FunctionEdit, PlanDraft, PlanReview
from app.agents.strategy import AgenticStrategy
from app.healing.loop import apply_and_verify, decide, propose_fix
from app.models.schemas import InvestigationReport, RootCauseReport
from app.models.schemas import WorkflowState as S
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store

GOOD_ADD = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


def src(name, needs, produces, value="x", framework="adk", log=None):
    def run(bb, records):
        if log is not None:
            log.append(name)
        return (value(bb) if callable(value) else value), f"{name} done"
    return KnowledgeSource(name, framework, tuple(needs), produces, run)


# --------------------------------------------------------------------------- board + controller


def test_board_keeps_every_post_and_reads_the_latest_live_one():
    bb = Blackboard(attempt=2)
    bb.post("plan_draft", {"v": 1}, "planner", "crewai", "first")
    bb.post("plan_draft", {"v": 2}, "planner", "crewai", "second")
    assert bb.get("plan_draft") == {"v": 2} and bb.count("plan_draft") == 2
    bb.retract("plan_draft")
    assert bb.get("plan_draft") == {"v": 1}  # retracting exposes the previous live post
    assert [e.seq for e in bb.entries] == [1, 2] and all(e.attempt == 2 for e in bb.entries)
    assert len(bb.drain()) == 2 and bb.drain() == []  # each post is persisted once


def test_controller_schedules_by_what_is_on_the_board_not_by_list_order():
    log = []
    sources = [  # deliberately listed out of order
        src("reviewer", ["draft"], "review", log=log),
        src("planner", ["cause"], "draft", log=log),
        src("analyst", ["evidence"], "cause", log=log),
    ]
    bb = Blackboard()
    bb.post("evidence", "E", "orchestrator", "system")
    assert BlackboardController(sources).run_until(bb, "review", []) == "x"
    assert log == ["analyst", "planner", "reviewer"]
    assert [e.author for e in bb.entries] == ["orchestrator", "analyst", "planner", "reviewer"]


def test_controller_stops_at_the_goal_and_resumes_later():
    log = []
    sources = [src("a", ["evidence"], "cause", log=log), src("b", ["cause"], "review", log=log)]
    bb = Blackboard()
    bb.post("evidence", "E", "orchestrator", "system")
    ctl = BlackboardController(sources)
    ctl.run_until(bb, "cause", [])
    assert log == ["a"]                      # analysis phase only
    ctl.run_until(bb, "review", [])
    assert log == ["a", "b"]                 # planning phase continues on the same board


def test_guard_rejection_posts_feedback_and_the_producer_retries_with_it():
    seen_feedback = []

    def plan(bb):
        seen_feedback.append(bb.get("feedback:draft"))
        return {"ok": len(seen_feedback) > 1}

    ctl = BlackboardController([src("planner", ["evidence"], "draft", value=plan)],
                               [Guard("draft", lambda c: [] if c["ok"] else ["edits a test file"])])
    bb = Blackboard()
    bb.post("evidence", "E", "orchestrator", "system")
    records: list[AgentRecord] = []
    assert ctl.run_until(bb, "draft", records) == {"ok": True}
    assert seen_feedback == [None, ["edits a test file"]]   # second try saw the guard's feedback
    keys = [(e.key, e.retracted) for e in bb.entries]
    assert keys == [("evidence", False), ("draft", True), ("feedback:draft", False), ("draft", False)]
    assert [r.status for r in records] == ["blocked"]


def test_guard_gives_up_after_max_retries():
    ctl = BlackboardController([src("planner", ["evidence"], "draft", value={"ok": False})],
                               [Guard("draft", lambda c: ["still wrong"])], max_retries=2)
    bb = Blackboard()
    bb.post("evidence", "E", "orchestrator", "system")
    with pytest.raises(ValueError, match="after 2 retries"):
        ctl.run_until(bb, "draft", [])
    assert bb.count("draft") == 3 and bb.count("feedback:draft") == 3


def test_controller_reports_when_nothing_can_contribute():
    bb = Blackboard()  # no evidence posted
    with pytest.raises(BlackboardStuck, match="missing: cause"):
        BlackboardController([src("a", ["evidence"], "cause")]).run_until(bb, "cause", [])


# --------------------------------------------------------------------------- strategy + persistence


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'bb.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


def fake_sources(first_plan_file="tests/test_calculator.py"):
    """Stand-ins for the 4 agents that read their inputs from the board, like the real ones."""
    tries = []

    def inv(bb):
        assert "test_add" in bb.get("evidence")
        return InvestigationReport(failure_summary="2 tests fail in add()", affected_files=["src/calculator.py"])

    def cause(bb):
        assert bb.get("investigation")["failure_summary"] == "2 tests fail in add()"
        return RootCauseReport(summary="s", root_cause="add() uses - instead of +", confidence=0.9)

    def draft(bb):
        tries.append(bb.get("feedback:plan_draft"))
        file = first_plan_file if len(tries) == 1 else "src/calculator.py"  # first try cheats on a test
        return PlanDraft(summary="restore +", root_cause=bb.get("root_cause")["root_cause"],
                         edits=[FunctionEdit(file=file, function="add" if file.startswith("src") else "test_add",
                                             new_source=GOOD_ADD if file.startswith("src")
                                             else "def test_add():\n    pass\n", description="d")])

    def review(bb):
        return PlanReview(approved=True, final_plan=PlanDraft.model_validate(bb.get("plan_draft")))

    return [src("investigator", ["evidence"], "investigation", inv),
            src("root_cause_analyst", ["evidence", "investigation"], "root_cause", cause),
            src("Repair Planner", ["evidence", "root_cause"], "plan_draft", draft, framework="crewai"),
            src("Code Reviewer", ["evidence", "root_cause", "plan_draft"], "review", review, framework="crewai")]


def test_blackboard_strategy_heals_and_the_board_is_persisted(store):
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    strategy = AgenticStrategy(sources=fake_sources(), messaging="blackboard", fallback=False)
    plan = propose_fix(store, wf, strategy)
    assert plan.strategy == "adk+crewai" and plan.changes[0].file == "src/calculator.py"
    decide(store, wf, True, "alice")
    assert apply_and_verify(store, wf, strategy=strategy) == S.PR_CREATED

    board = [a["payload"] for a in store.artifacts(wf, "blackboard")]
    assert [(e["key"], e["author"], e["retracted"]) for e in board] == [
        ("evidence", "orchestrator", False),
        ("investigation", "investigator", False),
        ("root_cause", "root_cause_analyst", False),
        ("plan_draft", "Repair Planner", True),          # cheated on a test -> retracted
        ("feedback:plan_draft", "plan guardrail", False),
        ("plan_draft", "Repair Planner", False),
        ("review", "Code Reviewer", False),
    ]
    assert [e["seq"] for e in board] == list(range(1, 8))
    blocked = [e for e in store.events(wf) if e["status"] == "blocked"]
    assert blocked and "not tests" in blocked[0]["message"]


def test_failed_agent_falls_back_and_the_board_says_who_did_it(store):
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id

    def broken(bb, records):
        raise RuntimeError("Gemini unavailable")

    sources = fake_sources()
    sources[0] = KnowledgeSource("investigator", "adk", ("evidence",), "investigation", broken)
    plan = propose_fix(store, wf, AgenticStrategy(sources=sources, messaging="blackboard", fallback=True))
    assert plan is not None
    board = [a["payload"] for a in store.artifacts(wf, "blackboard")]
    authors = {e["key"]: e["author"] for e in board}
    assert authors["investigation"] == "rule-based (fallback)" and authors["root_cause"] == "rule-based (fallback)"
    assert any(e["status"] == "fallback" for e in store.events(wf))


def test_unknown_messaging_is_rejected():
    with pytest.raises(ValueError, match="AGENT_MESSAGING"):
        AgenticStrategy(messaging="carrier-pigeon")
