"""Tracing and monitoring: span trees, callbacks -> spans, persistence, OTLP export, heat-map data."""

import threading
from dataclasses import replace

import pytest

import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod
from app.agents.blackboard import KnowledgeSource
from app.agents.models import FunctionEdit, PlanDraft, PlanReview
from app.agents.strategy import AgenticStrategy
from app.healing.loop import propose_fix
from app.models.schemas import InvestigationReport, RootCauseReport
from app.observability import tracing
from app.observability.monitoring import agent_runs, monitoring
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store

GOOD_ADD = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


def tree(spans):
    by_id = {s["span_id"]: s for s in spans}
    return {s["name"]: (by_id[s["parent_id"]]["name"] if s["parent_id"] in by_id else None) for s in spans}


# --------------------------------------------------------------------------- tracer


def test_spans_nest_and_record_errors():
    trace = tracing.Trace()
    with tracing.activate(trace):
        with tracing.span("outer", "phase"):
            with tracing.span("inner", "agent", "adk", model="m"):
                pass
            with pytest.raises(ValueError), tracing.span("broken", "tool"):
                raise ValueError("boom")
    spans = trace.drain()
    assert tree(spans) == {"outer": None, "inner": "outer", "broken": "outer"}
    broken = next(s for s in spans if s["name"] == "broken")
    assert broken["status"] == "error" and "boom" in broken["attributes"]["error"]
    assert all(s["trace_id"] == trace.trace_id and s["end"] >= s["start"] for s in spans)


def test_no_active_trace_means_no_spans_and_no_errors():
    with tracing.span("ignored", "agent") as s:
        assert s is None
    assert tracing.begin("k", "x", "llm") is None and tracing.finish("k") is None


def test_begin_finish_pairs_callbacks_and_nests_children():
    trace = tracing.Trace()
    with tracing.activate(trace):
        tracing.begin("agent:1", "investigator", "agent", "adk", nest=True)
        tracing.begin("llm:1", "LLM call", "llm", "adk")
        tracing.finish("llm:1", prompt_tokens=100, output_tokens=20)
        tracing.finish("agent:1")
        with tracing.span("after", "phase"):  # the parent was restored after the agent finished
            pass
    spans = trace.drain()
    assert tree(spans) == {"investigator": None, "LLM call": "investigator", "after": None}
    assert next(s for s in spans if s["kind"] == "llm")["attributes"] == {"prompt_tokens": 100, "output_tokens": 20}


def test_threads_add_to_one_trace_and_remote_spans_are_adopted():
    trace = tracing.Trace()
    with tracing.activate(trace), tracing.span("A2A call", "a2a") as hop:
        threads = [threading.Thread(target=lambda i=i: trace.add(f"t{i}", "tool", 0, 1, hop.span_id))
                   for i in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        remote = tracing.Trace()
        with tracing.activate(remote), tracing.span("served", "a2a"), tracing.span("agent", "agent"):
            pass
        trace.adopt(remote.drain(), hop.span_id)
    spans = trace.drain()
    assert sum(s["name"].startswith("t") for s in spans) == 5
    assert tree(spans)["served"] == "A2A call" and tree(spans)["agent"] == "served"
    assert {s["trace_id"] for s in spans} == {trace.trace_id}      # re-homed into the caller's trace


def test_unfinished_spans_are_closed_as_open():
    trace = tracing.Trace()
    trace.open("never finished", "llm")
    assert trace.drain()[0]["status"] == "open"


def test_otlp_export_has_the_standard_shape():
    trace = tracing.Trace()
    with tracing.activate(trace), tracing.span("root", "attempt"), tracing.span("llm", "llm", "adk", prompt_tokens=5):
        pass
    otlp = tracing.to_otlp(trace.drain())
    spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 2 and len(spans[0]["traceId"]) == 32 and len(spans[0]["spanId"]) == 16
    child = next(s for s in spans if s["name"] == "llm")
    assert child["parentSpanId"] == spans[0]["spanId"] if spans[0]["name"] == "root" else True
    assert {"key": "sem7.prompt_tokens", "value": {"intValue": "5"}} in child["attributes"]
    assert int(child["endTimeUnixNano"]) >= int(child["startTimeUnixNano"])


# --------------------------------------------------------------------------- strategy -> stored trace -> API


@pytest.fixture
def store(tmp_path, monkeypatch):
    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 't.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    return Store()


def traced_sources():
    """Fake agents that behave like the real ones on the trace: an agent span with an LLM call and a tool call."""

    def agent(name, framework, produce, tokens):
        def run(bb, records):
            with tracing.span(name, "agent", framework):
                with tracing.span(f"LLM call · {name}", "llm", framework, prompt_tokens=tokens, output_tokens=10):
                    pass
                with tracing.span("tool · read_source", "tool", "mcp"):
                    pass
            return produce(bb), f"{name} done"
        return run

    draft = PlanDraft(summary="restore +", root_cause="r", edits=[FunctionEdit(
        file="src/calculator.py", function="add", new_source=GOOD_ADD, description="d")])
    return [
        KnowledgeSource("investigator", "adk", ("evidence",), "investigation",
                        agent("investigator", "adk", lambda bb: InvestigationReport(failure_summary="2 fail"), 100)),
        KnowledgeSource("root_cause_analyst", "adk", ("evidence", "investigation"), "root_cause",
                        agent("root_cause_analyst", "adk",
                              lambda bb: RootCauseReport(summary="s", root_cause="add() uses -", confidence=0.9), 200)),
        KnowledgeSource("Repair Planner", "crewai", ("evidence", "root_cause"), "plan_draft",
                        agent("Repair Planner", "crewai", lambda bb: draft, 300)),
        KnowledgeSource("Code Reviewer", "crewai", ("evidence", "root_cause", "plan_draft"), "review",
                        agent("Code Reviewer", "crewai", lambda bb: PlanReview(approved=True, final_plan=draft), 400)),
    ]


def test_each_attempt_is_saved_as_one_trace_tree(store):
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    propose_fix(store, wf, AgenticStrategy(sources=traced_sources(), messaging="blackboard", fallback=False,
                                           pattern="sequential"))
    [art] = store.artifacts(wf, "trace")
    spans = art["payload"]["spans"]
    parents = tree(spans)
    assert parents["repair attempt 1"] is None
    assert parents["investigate"] == parents["plan"] == "repair attempt 1"
    assert parents["input guardrail"] == "investigate"
    assert parents["blackboard step · investigator"] == "investigate"
    assert parents["investigator"] == "blackboard step · investigator"
    assert parents["LLM call · Repair Planner"] == "Repair Planner"
    assert parents["guard · plan guardrail"] == "plan"
    root = next(s for s in spans if s["kind"] == "attempt")
    assert root["attributes"]["pattern"] == "sequential" and root["end"] >= max(s["end"] for s in spans) - 1e-3


def test_trace_and_monitoring_endpoints(store, monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.main as api

    monkeypatch.setattr(api, "get_store", lambda: store)
    for _ in range(2):
        wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
        propose_fix(store, wf, AgenticStrategy(sources=traced_sources(), messaging="blackboard", fallback=False))
    client = TestClient(api.app)
    body = client.get(f"/workflows/{wf}/trace").json()
    assert body["traces"][0]["attempt"] == 1 and len(body["traces"][0]["spans"]) > 10
    otlp = client.get(f"/workflows/{wf}/trace?format=otlp").json()
    assert len(otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]) == len(body["traces"][0]["spans"])
    assert client.get("/workflows/wf-nope/trace").status_code == 404

    mon = client.get("/monitoring/agents").json()
    assert len(mon["runs"]) == 2 and mon["runs"][0]["llm_calls"] == 4 and mon["runs"][0]["tokens"] == 1040
    rows = {r["agent"]: r for r in mon["agents"]}
    assert set(rows) == {"investigator", "root_cause_analyst", "Repair Planner", "Code Reviewer"}
    assert rows["Code Reviewer"]["runs"] == 2 and rows["Code Reviewer"]["avg_tokens"] == 410
    assert rows["investigator"]["avg_tool_calls"] == 1 and rows["investigator"]["framework"] == "adk"
    assert len(mon["cells"]) == 8                                   # 4 agents x 2 runs for the heat map


def test_llm_and_tool_calls_are_credited_to_their_agent():
    trace = tracing.Trace()
    with tracing.activate(trace), tracing.span("crew", "crew"):
        with tracing.span("Repair Lead", "agent"), tracing.span("delegate → Repair Planner", "delegation"):
            with tracing.span("Repair Planner", "agent"):
                with tracing.span("LLM call", "llm", prompt_tokens=50, output_tokens=5):
                    pass
        with tracing.span("stray LLM call", "llm", prompt_tokens=1):  # outside any agent: not credited
            pass
    rows = {r["agent"]: r for r in agent_runs(trace.drain())}
    assert rows["Repair Planner"]["llm_calls"] == 1 and rows["Repair Planner"]["tokens"] == 55
    assert rows["Repair Lead"]["llm_calls"] == 0                    # the coworker's call is the coworker's


def test_monitoring_is_empty_without_traces(store):
    run_ci_workflow("simple_bug", store, None, strategy=None)
    assert monitoring(store) == {"runs": [], "agents": [], "cells": []}
