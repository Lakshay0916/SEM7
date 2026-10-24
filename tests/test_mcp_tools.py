"""MCP tool logic (the functions behind sem7-risk and sem7-ci) - no MCP package or network needed."""

from dataclasses import replace

import pytest

import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod
from app.agents import mcp_tools
from app.ci.scenarios import get_scenario
from app.ci.simulator import create_sandbox
from app.git.repo import GitRepo
from app.healing.rules import RuleBasedStrategy
from app.mcp_servers import tools
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store

GOOD_ADD = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


@pytest.fixture
def scope(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'mcp.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    repo = create_sandbox(get_scenario("simple_bug"))
    g = GitRepo(repo)
    return tools.RepairScope(repo, g._run("rev-parse", "main").strip(), g.head_sha())


def test_scope_round_trips_through_the_server_environment(scope, monkeypatch):
    for k, v in scope.env().items():
        monkeypatch.setenv(k, v)
    assert tools.RepairScope.from_env() == scope


def test_score_change_risk_uses_the_trained_model(scope):
    r = tools.score_change_risk(scope)
    assert r["risk_level"] in {"LOW", "MEDIUM", "HIGH"} and 0 <= r["risk_score"] <= 1
    assert r["files_changed"] == ["src/calculator.py"] and r["model_version"].startswith("risk-")


def test_risk_model_card_reports_real_metrics():
    card = tools.risk_model_card()
    assert set(card["variants"]) == {"full", "diff_only"}
    assert card["variants"]["full"]["model"] == "logistic_regression"
    assert 0.5 < card["variants"]["full"]["test_roc_auc"] < 1


def test_read_source_versions_and_functions(scope):
    cur = tools.read_source(scope, "src/calculator.py", "add", "current")
    base = tools.read_source(scope, "src/calculator.py", "add", "baseline")
    assert "return a - b" in cur["source"] and "return a + b" in base["source"]
    assert "def subtract" in tools.read_source(scope, "src/calculator.py")["source"]  # whole file
    assert "error" in tools.read_source(scope, "src/calculator.py", "nope")
    assert "error" in tools.read_source(scope, "src/calculator.py", version="yesterday")
    with pytest.raises(ValueError, match="outside the repository"):
        tools.read_source(scope, "../../etc/passwd")


def test_dry_run_tests_a_fix_without_touching_the_repo(scope):
    before = (scope.repo_dir / "src" / "calculator.py").read_text()
    good = tools.run_tests_with_patch(scope, "src/calculator.py", "add", GOOD_ADD)
    bad = tools.run_tests_with_patch(scope, "src/calculator.py", "add", "def add(a, b):\n    return a * b\n")
    assert good["status"] == "PASS" and good["passed"] == 9
    assert bad["status"] == "FAIL" and any("test_add" in f["test"] for f in bad["failures"])
    assert (scope.repo_dir / "src" / "calculator.py").read_text() == before  # repo untouched
    assert "error" in tools.run_tests_with_patch(scope, "tests/test_calculator.py", "test_add", "def test_add(): pass")
    assert "error" in tools.run_tests_with_patch(scope, "src/calculator.py", "nope", GOOD_ADD)


def test_past_failures_searches_the_workflow_database(scope):
    store = Store()
    run_ci_workflow("simple_bug", store, None, strategy=RuleBasedStrategy())  # leaves a root cause in the DB
    hits = tools.past_failures("add operator")["matches"]
    assert hits and "add()" in hits[0]["root_cause"] and hits[0]["fixed_by"] == "rule-based"
    assert tools.past_failures("zzz-not-there")["matches"] == []


# --------------------------------------------------------------------------- agent wiring helpers


def test_every_agent_has_tools_and_a_hint_only_when_scoped(scope):
    assert set(mcp_tools.AGENT_TOOLS) == {"investigator", "root_cause_analyst", "Repair Planner", "Code Reviewer"}
    assert mcp_tools.hint("Repair Planner", None) == "" and "run_tests_with_patch" in mcp_tools.hint("Repair Planner", scope)
    env = mcp_tools.server_env(scope)
    assert env["SEM7_REPO_DIR"] == str(scope.repo_dir) and "PYTHONPATH" in env
    assert not any("API_KEY" in k for k in env)  # tool servers never receive LLM keys


def test_tool_results_are_unwrapped_and_errors_detected():
    wrapped = {"content": [{"type": "text", "text": '{"status": "PASS", "passed": 9}'}]}
    assert mcp_tools._unwrap(wrapped) == {"status": "PASS", "passed": 9}
    assert mcp_tools._summarise(wrapped) == '{"status": "PASS", "passed": 9}'
    assert mcp_tools._is_error({"error": "no such file"}) and mcp_tools._is_error("Error executing tool x")
    assert not mcp_tools._is_error(wrapped)
