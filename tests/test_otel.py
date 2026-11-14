"""OpenTelemetry: agent traces re-emitted as OTel spans, ADK duplicates dropped, metrics, instrumentation.

Uses OTel's in-memory exporter / reader, so nothing leaves the process.
"""

from dataclasses import replace

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import app.ci.simulator as sim_mod
import app.orchestration.store as store_mod
from app.observability import metrics, otel, tracing

SPANS = InMemorySpanExporter()
READER = InMemoryMetricReader()


@pytest.fixture(scope="module", autouse=True)
def providers():
    """One global tracer / meter provider for this module (OTel allows setting each only once)."""
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(otel._filtering(SPANS, otel.DROPPED_SCOPES)))
    trace.set_tracer_provider(tp)
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[READER]))
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    SPANS.clear()
    monkeypatch.setattr(otel, "_configured", True)  # as if setup() had run with an OTLP endpoint
    yield


def agent_trace():
    t = tracing.Trace()
    with tracing.activate(t), tracing.span("repair attempt 1", "attempt", "system", pattern="parallel"):
        with tracing.span("investigator", "agent", "adk"):
            with tracing.span("LLM call · investigator", "llm", "adk", model="gemini-3.1-flash-lite",
                              prompt_tokens=1000, output_tokens=40):
                pass
            with tracing.span("tool · read_source", "tool", "mcp"):
                pass
        with pytest.raises(RuntimeError), tracing.span("A2A → sem7-crewai/plan", "a2a", "a2a"):
            raise RuntimeError("429 rate limited")
    return t.drain()


def exported():
    return {s.name: s for s in SPANS.get_finished_spans()}


def metric_points(name):
    data = READER.get_metrics_data()
    return [p for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == name
            for p in m.data.data_points]


# --------------------------------------------------------------------------- traces


def test_agent_trace_becomes_otel_spans_under_the_active_request():
    spans = agent_trace()
    with trace.get_tracer("test").start_as_current_span("POST /workflows") as request:
        trace_id = otel.export_trace(spans)
    out = exported()
    assert trace_id == format(request.get_span_context().trace_id, "032x")       # one trace with the request
    assert out["repair attempt 1"].parent.span_id == request.get_span_context().span_id
    assert out["investigator"].parent.span_id == out["repair attempt 1"].context.span_id
    assert out["LLM call · investigator"].parent.span_id == out["investigator"].context.span_id
    src = {s["name"]: s for s in spans}
    assert out["investigator"].start_time == int(src["investigator"]["start"] * 1e9)   # real timings kept


def test_llm_spans_use_the_genai_conventions_and_errors_are_marked():
    otel.export_trace(agent_trace())
    out = exported()
    llm = out["LLM call · investigator"].attributes
    assert llm["gen_ai.request.model"] == "gemini-3.1-flash-lite" and llm["gen_ai.system"] == "gemini"
    assert llm["gen_ai.usage.input_tokens"] == 1000 and llm["gen_ai.usage.output_tokens"] == 40
    assert out["tool · read_source"].attributes["gen_ai.operation.name"] == "execute_tool"
    a2a = out["A2A → sem7-crewai/plan"]
    assert a2a.status.status_code == StatusCode.ERROR and "429" in a2a.status.description


def test_adk_builtin_spans_are_dropped_so_steps_appear_once():
    trace.get_tracer("gcp.vertex.agent").start_span("call_llm").end()   # what Google ADK emits itself
    trace.get_tracer("sem7.agents").start_span("LLM call · investigator").end()
    assert set(exported()) == {"LLM call · investigator"}


def test_nothing_happens_without_an_otlp_endpoint(monkeypatch):
    monkeypatch.setattr(otel, "_configured", False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert otel.setup("sem7-api") is False and otel.export_trace(agent_trace()) is None
    assert exported() == {}


def test_strategy_links_its_stored_trace_to_the_otel_trace(tmp_path, monkeypatch):
    from app.agents.strategy import AgenticStrategy
    from app.healing.loop import propose_fix
    from app.orchestration.pipeline import run_ci_workflow
    from app.orchestration.store import Store
    from tests.test_tracing import traced_sources

    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'o.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    store = Store()
    wf = run_ci_workflow("simple_bug", store, None, strategy=None).workflow_id
    propose_fix(store, wf, AgenticStrategy(sources=traced_sources(), messaging="blackboard", fallback=False))
    root = next(s for s in store.artifacts(wf, "trace")[0]["payload"]["spans"] if s["kind"] == "attempt")
    out = exported()
    assert root["attributes"]["otel_trace_id"] == format(out["repair attempt 1"].context.trace_id, "032x")
    assert {"input guardrail", "blackboard step · investigator", "LLM call · Repair Planner"} <= set(out)


# --------------------------------------------------------------------------- metrics


def test_attempt_metrics_come_from_the_trace():
    metrics.record_attempt(agent_trace())
    calls = metric_points("sem7.llm.calls")
    assert any(p.attributes == {"agent": "investigator", "model": "gemini-3.1-flash-lite"} and p.value >= 1
               for p in calls)
    tokens = {p.attributes["type"]: p.value for p in metric_points("sem7.llm.tokens")
              if p.attributes["agent"] == "investigator"}
    assert tokens["input"] >= 1000 and tokens["output"] >= 40
    cost = sum(p.value for p in metric_points("sem7.llm.cost"))
    assert cost >= 1000 / 1e6 * 0.25 + 40 / 1e6 * 1.50 - 1e-12
    assert any(p.attributes["agent"] == "investigator" and p.attributes["pattern"] == "parallel"
               for p in metric_points("sem7.agent.duration"))
    assert any(p.attributes == {"service": "sem7-crewai", "skill": "plan"} for p in metric_points("sem7.a2a.duration"))


def test_workflow_and_guardrail_metrics():
    metrics.record_workflow("pr_created", 2, "agents", "hierarchical", "failed_first_repair")
    metrics.record_guardrail("chat", "block")
    wf = [p for p in metric_points("sem7.workflows") if p.attributes["pattern"] == "hierarchical"]
    assert wf and wf[0].attributes["outcome"] == "pr_created"
    attempts = [p for p in metric_points("sem7.repair.attempts") if p.attributes["pattern"] == "hierarchical"]
    assert attempts and attempts[0].sum >= 2
    assert any(p.attributes == {"context": "chat", "action": "block"} for p in metric_points("sem7.guardrail.verdicts"))


# --------------------------------------------------------------------------- instrumentation


def test_api_and_a2a_apps_get_server_spans():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    api = FastAPI()

    @api.get("/workflows")
    def workflows():
        return []

    otel.instrument_app(api)
    TestClient(api).get("/workflows")
    assert any(n.startswith("GET /workflows") for n in exported())

    SPANS.clear()
    agent = Starlette(routes=[Route("/", lambda r: JSONResponse({}), methods=["POST"])])
    otel.instrument_app(agent)  # an A2A service: plain ASGI middleware
    TestClient(agent).post("/")
    assert any(n.startswith("POST") for n in exported())
