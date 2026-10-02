from app.ci.scenarios import SCENARIOS, get_scenario
from app.ci.simulator import create_sandbox, run_pipeline
from app.ci.test_runner import run_tests
from app.git.repo import GitRepo
from app.models.schemas import TestStatus


def test_all_scenarios_apply_cleanly(tmp_path):
    for s in SCENARIOS.values():
        create_sandbox(s, workspace=tmp_path)


def test_simple_bug_produces_failure_event(tmp_path):
    repo_dir = create_sandbox(get_scenario("simple_bug"), workspace=tmp_path)
    run = run_pipeline(repo_dir)
    ev = run.failure_event
    assert ev is not None
    assert ev.branch == "feature/refactor-add"
    assert ev.changed_files == ["src/calculator.py"]
    assert set(ev.failed_tests) == {
        "tests/test_calculator.py::test_add",
        "tests/test_calculator.py::test_add_negative",
    }
    assert run.diff.files[0].functions_changed == ["add"]
    assert "assert -1 == 5" in ev.logs


def test_failed_first_repair_has_two_defects(tmp_path):
    run = run_pipeline(create_sandbox(get_scenario("failed_first_repair"), workspace=tmp_path))
    names = {t.split("::")[-1] for t in run.failure_event.failed_tests}
    assert names == {"test_divide", "test_divide_negative", "test_divide_by_zero"}


def test_main_baseline_stays_healthy(tmp_path):
    repo_dir = create_sandbox(get_scenario("simple_bug"), workspace=tmp_path)
    repo = GitRepo(repo_dir)
    repo.checkout("main")
    assert run_tests(repo_dir).status == TestStatus.PASS


def test_high_risk_change_passes_tests_but_is_broad(tmp_path):
    run = run_pipeline(create_sandbox(get_scenario("high_risk_change"), workspace=tmp_path))
    assert run.test_report.status == TestStatus.PASS and run.failure_event is None
    by_path = {f.path: f for f in run.diff.files}
    assert by_path["requirements.txt"].is_dependency and by_path["pytest.ini"].is_config
    assert set(by_path["src/calculator.py"].functions_changed) == {"add", "subtract", "multiply", "average"}
    assert "src/session_token.py" in by_path
