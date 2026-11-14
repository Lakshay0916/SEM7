"""OpenTelemetry: traces, metrics and logs from every container, exported over OTLP to the Collector.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so tests, CI and plain local runs export nothing. When on:

* auto-instrumentation - every FastAPI request, SQLAlchemy query and httpx call is a span; httpx and the
  A2A services carry W3C trace context, so the agent containers join the API's trace;
* agent traces - the spans recorded by the ADK / CrewAI callbacks (app/observability/tracing.py: agents,
  LLM calls with tokens, MCP tools, delegations, A2A hops, guardrails) are re-emitted as OTel spans under
  the request that ran them; LLM calls use the GenAI semantic conventions (gen_ai.*);
* metrics (app/observability/metrics.py) leave on the same pipeline;
* log lines carry the active trace and span id.

Google ADK also emits its own OTel spans for the same agent / LLM / tool steps; those are dropped so each
step appears once - ours carry the tokens and the blackboard context.
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

log = logging.getLogger(__name__)
DROPPED_SCOPES = {"gcp.vertex.agent"}  # Google ADK's built-in tracer: duplicates our callback spans
_configured = False


def enabled() -> bool:
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def _filtering(exporter, drop: set[str]):
    """Wrap a span exporter so spans from the given instrumentation scopes are not exported."""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class Filtering(SpanExporter):
        def export(self, spans: Sequence) -> SpanExportResult:
            keep = [s for s in spans if getattr(s.instrumentation_scope, "name", "") not in drop]
            return exporter.export(keep) if keep else SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            exporter.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return exporter.force_flush(timeout_millis)

    return Filtering()


def setup(service: str, app=None) -> bool:
    """Configure OTel for this process once (no-op without an OTLP endpoint). Returns whether it is on."""
    global _configured
    if _configured or not enabled():
        return _configured
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service, "service.namespace": "sem7",
                                "deployment.environment": os.getenv("SEM7_ENV", "local")})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(_filtering(OTLPSpanExporter(), DROPPED_SCOPES)))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[
        PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15000)]))

    LoggingInstrumentor().instrument(set_logging_format=True)  # trace_id / span_id in every log line
    HTTPXClientInstrumentor().instrument()                     # outgoing calls, incl. A2A hops
    SQLAlchemyInstrumentor().instrument()                      # every database query
    if app is not None:
        instrument_app(app)
    _configured = True
    log.info("OpenTelemetry on: service %s -> %s", service, os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    return True


def instrument_app(app) -> None:
    """Server spans for a FastAPI app (the API) or any ASGI app (the A2A agent services).

    The per-message "http send / http receive" spans of the ASGI layer are left out: noise, no insight."""
    if getattr(app, "router", None) is not None and hasattr(app, "openapi"):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health", exclude_spans=["receive", "send"])
    else:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        app.add_middleware(OpenTelemetryMiddleware, excluded_urls="health", exclude_spans=["receive", "send"])


# --------------------------------------------------------------------------- agent traces -> OTel spans


GENAI = {"model": "gen_ai.request.model", "prompt_tokens": "gen_ai.usage.input_tokens",
         "output_tokens": "gen_ai.usage.output_tokens"}


def _attributes(span: dict) -> dict:
    attrs = {"sem7.kind": span["kind"], "sem7.framework": span.get("framework") or ""}
    for k, v in (span.get("attributes") or {}).items():
        if v is None or isinstance(v, (dict, list)):
            continue
        attrs[GENAI.get(k, f"sem7.{k}") if span["kind"] == "llm" else f"sem7.{k}"] = v
    if span["kind"] == "llm":
        attrs.update({"gen_ai.system": "gemini", "gen_ai.operation.name": "chat"})
    if span["kind"] == "tool":
        attrs["gen_ai.operation.name"] = "execute_tool"
    return attrs


def export_trace(spans: list[dict], tracer=None) -> str | None:
    """Re-emit a finished agent trace as OTel spans, under the currently active span (the request).

    Returns the OTel trace id (hex) of the new spans, or None when OTel is off."""
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    if tracer is None:
        if not _configured:
            return None
        tracer = trace.get_tracer("sem7.agents")
    by_parent: dict = {}
    ids = {s["span_id"] for s in spans}
    for s in sorted(spans, key=lambda s: s["start"]):
        by_parent.setdefault(s["parent_id"] if s["parent_id"] in ids else None, []).append(s)

    trace_id = None

    def emit(parent_key, context):
        nonlocal trace_id
        for s in by_parent.get(parent_key, []):
            otel = tracer.start_span(s["name"], context=context, start_time=int(s["start"] * 1e9),
                                     attributes=_attributes(s))
            if s["status"] == "error":
                otel.set_status(Status(StatusCode.ERROR, str((s.get("attributes") or {}).get("error", ""))[:200]))
            trace_id = trace_id or format(otel.get_span_context().trace_id, "032x")
            emit(s["span_id"], trace.set_span_in_context(otel))
            otel.end(end_time=int((s["end"] or s["start"]) * 1e9))

    emit(None, None)  # None = the active context, i.e. under the HTTP request span
    return trace_id
