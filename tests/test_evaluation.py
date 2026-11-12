"""Agent evaluation: the run matrix, real end-to-end runs measured from their records, summary and report."""

import json
from dataclasses import replace

import pytest

import app.ci.simulator as sim_mod
import app.evaluation.runner as runner
import app.orchestration.store as store_mod
from app.evaluation.runner import RunSpec, matrix, run_one
from app.evaluation.summary import _quartiles, report, summarize
from app.orchestration.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'e.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


def test_matrix_covers_every_configuration_evenly():
    runs = matrix(3)
    assert len(runs) == 42 and len({r.run_id for r in runs}) == 42
    agents = [r for r in runs if r.group == "agents"]
    assert len(agents) == 36
    configs = {(r.pattern, r.messaging, r.scenario) for r in agents}
    assert len(configs) == 12 and all(sum((r.pattern, r.messaging, r.scenario) == c for r in agents) == 3
                                      for c in configs)
    assert [r.group for r in runs[:6]] == ["rules"] * 4 + ["guardrail"] * 2   # cheap runs first


def test_rule_baseline_run_is_measured_end_to_end(store):
    row = run_one(store, RunSpec("rules-x", "rules", "failed_first_repair"))
    assert row["success"] and row["final_state"] == "PR_CREATED"
    assert row["attempts"] == 2 and not row["first_try"]           # the rules need reflection here
    assert row["plans_by_rules"] == 2 and row["tokens"] == 0 and row["cost_usd"] == 0
    assert row["root_cause_correct"] and row["analysis_seconds"] > 0 and row["wall_seconds"] >= row["analysis_seconds"]


def test_guardrail_run_blocks_before_any_llm_call(store):
    row = run_one(store, RunSpec("g", "guardrail", "injected_commit", "sequential", "blackboard"))
    assert row["guardrail_blocked"] and row["success"]
    assert row["llm_calls"] == 0 and row["plans_by_agents"] == 0 and not row["agent_success"]


def test_agent_run_takes_tokens_and_cost_from_its_trace(store, monkeypatch):
    from app.agents.strategy import AgenticStrategy
    from tests.test_tracing import traced_sources

    monkeypatch.setattr(runner, "make_strategy", lambda spec: AgenticStrategy(
        sources=traced_sources(), messaging="blackboard", fallback=False))
    row = run_one(store, RunSpec("a", "agents", "simple_bug", "sequential", "blackboard"))
    assert row["agent_success"] and row["first_try"] and row["plans_by_agents"] == 1
    assert row["llm_calls"] == 4 and row["prompt_tokens"] == 1000 and row["output_tokens"] == 40
    assert row["cost_usd"] == pytest.approx(1000 / 1e6 * 0.25 + 40 / 1e6 * 1.50)
    assert row["tool_calls"] == 4 and row["agents_run"] == 4 and not row["daily_quota_hit"]


def test_daily_quota_errors_are_recognised(store, monkeypatch):
    from app.agents.strategy import AgenticStrategy

    def exhausted(evidence, records):
        raise RuntimeError("429 RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    monkeypatch.setattr(runner, "make_strategy", lambda spec: AgenticStrategy(
        analysts=exhausted, planner=lambda *a: None, messaging="direct", fallback=True))
    row = run_one(store, RunSpec("q", "agents", "simple_bug", "sequential", "direct"))
    assert row["daily_quota_hit"] and row["fallbacks"] >= 1      # the script will not record this run


def test_quartiles_and_summary_tables():
    assert _quartiles([1, 2, 3, 4, 5]) == {"median": 3, "q1": 2, "q3": 4, "min": 1, "max": 5}
    assert _quartiles([])["median"] is None and _quartiles([7])["q3"] == 7
    rows = [{"run_id": f"agents-simple_bug-{p}-{m}-1", "group": "agents", "pattern": p, "messaging": m,
             "scenario": "simple_bug", "success": True,
             "agent_success": p != "hierarchical", "first_try": True, "root_cause_correct": True, "fallbacks": 0,
             "llm_errors": 0, "cost_usd": 0.004, "analysis_seconds": 20.0, "wall_seconds": 25.0, "tokens": 10000,
             "llm_calls": 9, "tool_calls": 5} for p in ("sequential", "hierarchical") for m in ("blackboard", "direct")]
    s = summarize(rows)
    assert s["patterns"]["hierarchical"]["agent_success_rate"] == 0 and s["patterns"]["sequential"]["runs"] == 2
    assert s["totals"]["cost_usd"] == pytest.approx(0.016) and len(s["configurations"]) == 4
    md = report(s, rows)
    assert "## Agent patterns" in md and "| hierarchical | 2 | 100% | 0% |" in md
    assert "## Findings" in md and "fixed 2 of 4 runs on their own" in md     # computed from the rows


def test_evaluation_endpoint_and_page(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.main as api

    rows = [{"run_id": f"agents-simple_bug-{p}-blackboard-1", "group": "agents", "scenario": "simple_bug",
             "pattern": p, "messaging": "blackboard", "repeat": 1, "success": True, "agent_success": True,
             "first_try": True, "root_cause_correct": True, "fallbacks": 0, "llm_errors": 0, "cost_usd": 0.004,
             "analysis_seconds": 20.0 + i, "wall_seconds": 25.0, "tokens": 10000 + i, "llm_calls": 9, "tool_calls": 5,
             "attempts": 1, "final_state": "PR_CREATED"} for i, p in enumerate(("sequential", "parallel", "hierarchical"))]
    (tmp_path / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "summary.json").write_text(json.dumps(summarize(rows)))
    monkeypatch.setattr(api, "EVAL_DIR", tmp_path)
    body = TestClient(api.app).get("/evaluation").json()
    assert len(body["runs"]) == 3 and body["summary"]["runs"] == 3
    monkeypatch.setattr(api, "EVAL_DIR", tmp_path / "missing")
    assert TestClient(api.app).get("/evaluation").json() == {"runs": [], "summary": None}
