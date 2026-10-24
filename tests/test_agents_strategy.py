"""Agentic strategy logic with fake agents - no LLM, no network, no agent frameworks needed."""

from dataclasses import replace

import pytest

import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod
from app.agents.evidence import build_evidence
from app.agents.guardrails import check_draft, to_repair_plan
from app.agents.models import AgentRecord, FunctionEdit, PlanDraft
from app.agents.retry import is_transient, with_retries
from app.agents.strategy import AgenticStrategy
from app.healing.factory import make_strategy
from app.healing.loop import apply_and_verify, build_context, decide
from app.healing.patcher import validate_plan
from app.healing.rules import RuleBasedStrategy
from app.models.schemas import WorkflowState as S
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store

GOOD_ADD = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'agents.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


@pytest.fixture
def failed_ctx(store):
    """A simple_bug workflow stopped at FAILED (no strategy yet), and its repair context."""
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    return wf, build_context(store, wf)


def fake_analysts(evidence, records):
    rules = RuleBasedStrategy()
    records.append(AgentRecord(agent="investigator", framework="adk", seconds=0.1, prompt_tokens=10, output_tokens=5))
    fake_analysts.seen = evidence
    ctx = fake_analysts.ctx
    inv = rules.investigate(ctx)
    return inv, rules.root_cause(ctx, inv)


def fake_planner(evidence, investigation, root_cause, ctx, records):
    records.append(AgentRecord(agent="Repair Planner", framework="crewai", seconds=0.2))
    return PlanDraft(summary="Restore addition in add()", root_cause=root_cause.root_cause,
                     edits=[FunctionEdit(file="src/calculator.py", function="add", new_source=GOOD_ADD,
                                         description="use + again")])


# --------------------------------------------------------------------------- evidence & guardrails


def test_evidence_contains_the_real_facts(failed_ctx):
    _, ctx = failed_ctx
    ev = build_evidence(ctx)
    assert "tests/test_calculator.py::test_add" in ev and "assert -1 == 5" in ev   # failing test + message
    assert "def test_add():" in ev                                                  # test source
    assert "-    return a + b" in ev and "+    return a - b" in ev                  # developer's diff
    assert "Current content of `src/calculator.py`" in ev and "Baseline version of `add()`" in ev


@pytest.mark.parametrize("edit, problem", [
    (FunctionEdit(file="src/calculator.py", function="add", new_source=GOOD_ADD, description="ok"), None),
    (FunctionEdit(file="tests/test_calculator.py", function="test_add", new_source="def test_add():\n    pass\n",
                  description="cheat"), "not tests"),
    (FunctionEdit(file="src/calculator.py", function="nope", new_source="def nope():\n    pass\n",
                  description="x"), "function not found"),
    (FunctionEdit(file="src/calculator.py", function="add", new_source="def add(a, b)\n    return a + b\n",
                  description="x"), "not valid Python"),
    (FunctionEdit(file="src/calculator.py", function="add", new_source="def plus(a, b):\n    return a + b\n",
                  description="x"), "exactly the function"),
    (FunctionEdit(file="src/calculator.py", function="add",
                  new_source='def add(a, b):\n    """Return the sum of a and b."""\n    return a - b\n',
                  description="x"), "identical"),
    (FunctionEdit(file="../outside.py", function="add", new_source=GOOD_ADD, description="x"), "does not exist"),
])
def test_check_draft_guardrail(failed_ctx, edit, problem):
    _, ctx = failed_ctx
    problems = check_draft(PlanDraft(summary="s", root_cause="r", edits=[edit]), ctx.repo_dir)
    assert (problems == []) if problem is None else any(problem in p for p in problems), problems


def test_plan_original_text_comes_from_the_repo_not_the_model(failed_ctx):
    _, ctx = failed_ctx
    rules = RuleBasedStrategy()
    inv = rules.investigate(ctx)
    rc = rules.root_cause(ctx, inv)
    indented = "    " + GOOD_ADD.replace("\n    ", "\n        ")  # model indents differently
    draft = PlanDraft(summary="s", root_cause="r",
                      edits=[FunctionEdit(file="src/calculator.py", function="add", new_source=indented,
                                          description="d")])
    plan = to_repair_plan(draft, ctx, rc, "adk+crewai", "llm-agent")
    assert plan.changes[0].original.endswith("return a - b\n") and plan.changes[0].replacement == GOOD_ADD
    assert plan.strategy_kind == "llm-agent" and plan.level == "llm-attempt-1"
    validate_plan(ctx.repo_dir, plan)  # passes the pipeline's own safety checks


# --------------------------------------------------------------------------- strategy in the real loop


def test_agentic_strategy_heals_through_the_unchanged_loop(store, failed_ctx):
    wf, ctx = failed_ctx
    fake_analysts.ctx = ctx
    strategy = AgenticStrategy(analysts=fake_analysts, planner=fake_planner, fallback=False)
    from app.healing.loop import propose_fix

    plan = propose_fix(store, wf, strategy)
    assert plan.strategy == "adk+crewai" and store.state(wf) == S.WAITING_APPROVAL  # still needs a human
    decide(store, wf, True, "alice")
    assert apply_and_verify(store, wf, strategy=strategy) == S.PR_CREATED
    agent_events = [e for e in store.events(wf) if e["agent"].startswith(("adk:", "crewai:"))]
    assert [e["agent"] for e in agent_events] == ["adk:investigator", "crewai:Repair Planner"]
    assert "[10+5 tokens]" in agent_events[0]["message"] and agent_events[0]["duration_ms"] == 100


def test_analysis_failure_falls_back_to_rules_and_is_logged(store, failed_ctx):
    wf, _ = failed_ctx

    def broken(evidence, records):
        raise RuntimeError("model unavailable")

    from app.healing.loop import propose_fix

    plan = propose_fix(store, wf, AgenticStrategy(analysts=broken, planner=fake_planner, fallback=True))
    assert plan is not None and plan.strategy == "adk+crewai"  # planning still by the crew
    fallback = [e for e in store.events(wf) if e["status"] == "fallback"]
    assert fallback and "model unavailable" in fallback[0]["message"]


def test_invalid_agent_plan_is_blocked_then_rules_take_over(store, failed_ctx):
    wf, ctx = failed_ctx
    fake_analysts.ctx = ctx

    def cheating(evidence, inv, rc, ctx, records):
        return PlanDraft(summary="edit the test", root_cause="r", edits=[FunctionEdit(
            file="tests/test_calculator.py", function="test_add", new_source="def test_add():\n    pass\n",
            description="make it pass")])

    from app.healing.loop import propose_fix

    plan = propose_fix(store, wf, AgenticStrategy(analysts=fake_analysts, planner=cheating, fallback=True))
    assert plan.strategy == "rule-based"
    statuses = [e["status"] for e in store.events(wf)]
    assert "blocked" in statuses and "fallback" in statuses


def test_without_fallback_agent_errors_surface(failed_ctx):
    _, ctx = failed_ctx

    def broken(evidence, records):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        AgenticStrategy(analysts=broken, planner=fake_planner, fallback=False).investigate(ctx)


# --------------------------------------------------------------------------- factory & retries


def test_factory_defaults_to_rules_and_rejects_unknown():
    assert isinstance(make_strategy(), RuleBasedStrategy)  # CI / Docker default
    assert make_strategy("agents").kind == "llm-agent"
    with pytest.raises(ValueError):
        make_strategy("magic")


def test_transient_errors_are_retried_and_recorded():
    calls, records = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("503 UNAVAILABLE: high demand")
        return "ok"

    assert with_retries(flaky, "a", "adk", records, sleep=lambda s: None) == "ok"
    assert len(calls) == 3 and [r.status for r in records] == ["retry", "retry"]
    assert not is_transient(ValueError("bad schema"))
    with pytest.raises(ValueError):
        with_retries(lambda: (_ for _ in ()).throw(ValueError("bad schema")), "a", "adk", [], sleep=lambda s: None)
