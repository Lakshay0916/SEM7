import pytest

from app.ci.quality_gate import evaluate_gate
from app.git.repo import GitRepo
from app.healing.code_utils import function_source, operator_swap
from app.healing.loop import HealingError, apply_and_verify, build_context, decide, propose_fix, repair_branch
from app.healing.patcher import UnsafePlanError, validate_plan
from app.healing.rules import LEVEL_FUNCTION, LEVEL_OPERATOR, RuleBasedStrategy
from app.models.schemas import (
    GateStatus,
    ProposedChange,
    RepairPlan,
    RiskAssessment,
    RiskLevel,
    TestReport,
    TestStatus,
)
from app.models.schemas import WorkflowState as S
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store
from app.reporting.markdown import render_workflow_report


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'heal.sqlite3'}")


def _failed_workflow(store, tmp_path, scenario):
    return run_ci_workflow(scenario, store, workspace=tmp_path).workflow_id


def _approve_all(store, wf, strategy, **kw):
    levels = []
    while store.state(wf) == S.WAITING_APPROVAL:
        levels.append(store.latest(wf, "repair_plan")["level"])
        decide(store, wf, True, "tester")
        apply_and_verify(store, wf, strategy=strategy, **kw)
    return levels


# ------------------------------------------------------------------ code utils


def test_operator_swap():
    assert operator_swap("    return a + b", "    return a - b") == [("+", "-")]
    assert operator_swap("return a / b", "return a // b") == [("/", "//")]
    assert operator_swap("return a + b", "return a + c") is None  # name change, not operator
    assert operator_swap("x = 1", "x = 1") is None


def test_function_source():
    src = "class C:\n    @staticmethod\n    def m():\n        return 1\n\ndef f():\n    return 2\n"
    assert function_source(src, "f") == "def f():\n    return 2\n"
    assert function_source(src, "C.m").startswith("    @staticmethod")
    assert function_source(src, "missing") is None


# ------------------------------------------------------------------ quality gate


def _report(status, passed=5, failed=0):
    return TestReport(status=status, command="pytest", exit_code=0 if status == TestStatus.PASS else 1,
                      tests_run=passed + failed, tests_passed=passed, tests_failed=failed)


def _risk(level):
    return RiskAssessment(commit_sha="x", risk_score=0.9 if level == RiskLevel.HIGH else 0.1,
                          risk_level=level, model_version="v")


@pytest.mark.parametrize("status", [TestStatus.FAIL, TestStatus.ERROR, TestStatus.TIMEOUT])
def test_gate_blocks_any_non_pass_even_when_low_risk(status):
    assert evaluate_gate(_report(status, failed=1), _risk(RiskLevel.LOW)).status == GateStatus.BLOCK


def test_gate_high_risk_passing_tests_needs_review():
    assert evaluate_gate(_report(TestStatus.PASS), _risk(RiskLevel.HIGH)).status == GateStatus.REVIEW


def test_gate_pass():
    assert evaluate_gate(_report(TestStatus.PASS), _risk(RiskLevel.MEDIUM)).status == GateStatus.PASS
    assert evaluate_gate(_report(TestStatus.PASS), None).status == GateStatus.PASS


# ------------------------------------------------------------------ self-healing loop


def test_simple_bug_healed_in_one_minimal_attempt(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "simple_bug")
    strategy = RuleBasedStrategy()
    plan = propose_fix(store, wf, strategy)
    assert store.state(wf) == S.WAITING_APPROVAL
    assert plan.level == LEVEL_OPERATOR

    repo = GitRepo(store.get_workflow(wf)["repo_path"])
    main_before = repo.head_sha("main")
    feature_before = repo.head_sha("feature/refactor-add")

    assert _approve_all(store, wf, strategy) == [LEVEL_OPERATOR]
    assert store.state(wf) == S.PR_CREATED
    assert repo.head_sha("main") == main_before  # main untouched
    assert repo.head_sha("feature/refactor-add") == feature_before  # developer branch untouched
    assert repo.branch_exists(repair_branch(wf))
    attempt = store.latest(wf, "repair_attempt")
    assert attempt["changed_files"] == ["src/calculator.py"]
    assert "-    return a - b" in attempt["patch"] and "+    return a + b" in attempt["patch"]
    pr = store.latest(wf, "pull_request")
    assert pr["merged"] is False and pr["head_branch"] == repair_branch(wf)


def test_failed_first_repair_reflects_then_escalates(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "failed_first_repair")
    strategy = RuleBasedStrategy()
    propose_fix(store, wf, strategy)
    assert _approve_all(store, wf, strategy) == [LEVEL_OPERATOR, LEVEL_FUNCTION]
    assert store.state(wf) == S.PR_CREATED

    reflection = store.latest(wf, "reflection")
    assert reflection["still_failing"] == ["tests/test_calculator.py::test_divide_by_zero"]
    assert len(reflection["newly_fixed"]) == 2 and reflection["regressions"] == []
    assert [a["payload"]["approved"] for a in store.artifacts(wf, "approval")] == [True, True]
    assert [t["payload"]["status"] for t in store.artifacts(wf, "test_run")] == ["FAIL", "PASS"]
    assert "Attempt 2" in render_workflow_report(store.trace(wf))


def test_no_repair_without_approval(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "simple_bug")
    propose_fix(store, wf, RuleBasedStrategy())
    with pytest.raises(HealingError):
        apply_and_verify(store, wf)
    assert store.artifacts(wf, "repair_attempt") == []


def test_rejection_changes_nothing(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "simple_bug")
    propose_fix(store, wf, RuleBasedStrategy())
    decide(store, wf, False, "reviewer", "not convinced")
    assert store.state(wf) == S.REJECTED
    repo = GitRepo(store.get_workflow(wf)["repo_path"])
    assert not repo.branch_exists(repair_branch(wf))
    with pytest.raises(HealingError):
        apply_and_verify(store, wf)


def test_attempt_limit_aborts(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "failed_first_repair")
    strategy = RuleBasedStrategy()
    propose_fix(store, wf, strategy)
    _approve_all(store, wf, strategy, max_attempts=1)
    assert store.state(wf) == S.ABORTED
    assert store.latest(wf, "reflection")["will_retry"] is False
    assert len(store.artifacts(wf, "repair_attempt")) == 1


def test_strategy_gives_up_when_levels_exhausted(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "simple_bug")
    strategy = RuleBasedStrategy()
    ctx = build_context(store, wf)
    ctx.previous_plans = [RepairPlan(summary="", root_cause="", level=LEVEL_OPERATOR),
                          RepairPlan(summary="", root_cause="", level=LEVEL_FUNCTION)]
    inv = strategy.investigate(ctx)
    assert strategy.plan(ctx, inv, strategy.root_cause(ctx, inv)) is None


def test_investigation_separates_facts_from_hypotheses(store, tmp_path):
    wf = _failed_workflow(store, tmp_path, "simple_bug")
    inv = RuleBasedStrategy().investigate(build_context(store, wf))
    assert all(e.startswith("FACT:") for e in inv.evidence)
    assert all(h.startswith("HYPOTHESIS:") for h in inv.initial_hypotheses)
    assert inv.affected_files == ["src/calculator.py"]


# ------------------------------------------------------------------ patcher safety


@pytest.mark.parametrize("file,original,error", [
    ("tests/test_calculator.py", "assert add(2, 3) == 5", "may not modify tests"),
    ("pytest.ini", "testpaths", "config"),
    ("../outside.py", "x", "outside"),
    ("src/calculator.py", "return", "found"),  # ambiguous: occurs many times
    ("src/calculator.py", "this text does not exist", "found 0 times"),
])
def test_unsafe_plans_rejected(store, tmp_path, file, original, error):
    wf = _failed_workflow(store, tmp_path, "simple_bug")
    repo_dir = GitRepo(store.get_workflow(wf)["repo_path"]).path
    plan = RepairPlan(summary="s", root_cause="r",
                      changes=[ProposedChange(file=file, description="d", original=original, replacement="y")])
    with pytest.raises(UnsafePlanError, match=error):
        validate_plan(repo_dir, plan)
