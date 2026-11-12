"""Tracing: every step of an agent run as a timed span, in OpenTelemetry's model.

One repair attempt = one trace. Its spans form a tree:

    attempt 1                                   (the strategy)
    ├─ input guardrail
    ├─ investigate / plan                       (phases)
    │  ├─ blackboard step: investigator         (controller)
    │  │  ├─ A2A sem7-adk/investigator          (hop to an agent service; its spans come back with it)
    │  │  │  └─ ADK runner → investigator       (agent, from ADK agent callbacks)
    │  │  │     ├─ LLM call                     (from ADK model callbacks: model, tokens)
    │  │  │     └─ tool score_change_risk       (MCP, from ADK tool callbacks)
    │  │  ...
    │  └─ guard plan_draft                      (output guardrail verdict)

Spans are opened with `span()` (a context manager) or `begin()` / `finish()` (for frameworks that give
before/after callbacks); the active trace and parent travel in a context variable, so asyncio tasks
(ADK's ParallelAgent branches) and worker threads (A2A skills) nest correctly. Spans from another
process are merged with `Trace.adopt`. `to_otlp` exports a trace as OTLP JSON for any OpenTelemetry tool.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator

KINDS = ("attempt", "phase", "guardrail", "step", "a2a", "crew", "agent", "delegation", "llm", "tool")


def _id(n: int = 16) -> str:
    return uuid.uuid4().hex[:n]


@dataclass
class Span:
    name: str
    kind: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start: float                # epoch seconds
    end: float | None = None
    framework: str = ""
    status: str = "ok"          # ok | error | open (never finished)
    attributes: dict = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return round((self.end if self.end is not None else time.time()) - self.start, 3)

    def to_dict(self) -> dict:
        return {**asdict(self), "seconds": self.seconds}


class Trace:
    """The spans of one trace; safe to add to from several threads."""

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or _id(32)
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    def open(self, name: str, kind: str, parent_id: str | None = None, framework: str = "",
             start: float | None = None, **attributes) -> Span:
        s = Span(name, kind, self.trace_id, _id(), parent_id, start if start is not None else time.time(),
                 framework=framework, attributes={k: v for k, v in attributes.items() if v is not None})
        with self._lock:
            self.spans.append(s)
        return s

    def add(self, name: str, kind: str, start: float, end: float, parent_id: str | None = None,
            framework: str = "", status: str = "ok", **attributes) -> Span:
        s = self.open(name, kind, parent_id, framework, start, **attributes)
        s.end, s.status = end, status
        return s

    def adopt(self, spans: list[dict], parent_id: str | None) -> None:
        """Merge spans recorded in another process (an A2A agent service) under `parent_id`."""
        ids = {s["span_id"] for s in spans}
        with self._lock:
            for d in spans:
                d = {k: v for k, v in d.items() if k != "seconds"}
                parent = d.get("parent_id") if d.get("parent_id") in ids else parent_id
                self.spans.append(Span(**{**d, "trace_id": self.trace_id, "parent_id": parent}))

    def drain(self) -> list[dict]:
        """All spans as dicts (unfinished ones are closed now and marked `open`)."""
        now = time.time()
        with self._lock:
            for s in self.spans:
                if s.end is None:
                    s.end, s.status = now, "open"
            out = [s.to_dict() for s in sorted(self.spans, key=lambda s: s.start)]
            self.spans = []
        return out


_current: contextvars.ContextVar[tuple[Trace, str | None] | None] = contextvars.ContextVar("sem7_trace", default=None)
_begun: dict[str, tuple[Span, contextvars.Token | None, Trace]] = {}
_begun_lock = threading.Lock()


def current() -> tuple[Trace | None, str | None]:
    ctx = _current.get()
    return ctx if ctx is not None else (None, None)


@contextmanager
def activate(trace: Trace, parent_id: str | None = None) -> Iterator[Trace]:
    """Make `trace` (and `parent_id` as the parent span) active for everything run inside the block."""
    token = _current.set((trace, parent_id))
    try:
        yield trace
    finally:
        _current.reset(token)


@contextmanager
def span(name: str, kind: str, framework: str = "", **attributes) -> Iterator[Span | None]:
    """A child span of the active one around the block; does nothing when no trace is active."""
    trace, parent = current()
    if trace is None:
        yield None
        return
    s = trace.open(name, kind, parent, framework, **attributes)
    token = _current.set((trace, s.span_id))
    try:
        yield s
    except BaseException as exc:
        s.status = "error"
        s.attributes["error"] = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        s.end = time.time()
        _current.reset(token)


def begin(key: str, name: str, kind: str, framework: str = "", nest: bool = False, **attributes) -> Span | None:
    """Open a span from a framework's "before" callback; `finish(key)` from the matching "after" one.

    With `nest`, spans opened later in the same context (e.g. an agent's LLM calls) become its children."""
    trace, parent = current()
    if trace is None:
        return None
    s = trace.open(name, kind, parent, framework, **attributes)
    token = _current.set((trace, s.span_id)) if nest else None
    with _begun_lock:
        _begun[key] = (s, token, trace)
    return s


def finish(key: str, status: str = "ok", **attributes) -> Span | None:
    with _begun_lock:
        s, token, trace = _begun.pop(key, (None, None, None))
    if s is None:
        return None
    s.end, s.status = time.time(), status
    s.attributes.update({k: v for k, v in attributes.items() if v is not None})
    if token is not None:
        try:
            _current.reset(token)
        except ValueError:  # "after" ran in another context than "before": restore the parent explicitly
            _current.set((trace, s.parent_id))
    return s


# --------------------------------------------------------------------------- export


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def to_otlp(spans: list[dict], service: str = "sem7-agents") -> dict:
    """OTLP JSON (the OpenTelemetry wire format), importable by Jaeger, Grafana Tempo, Honeycomb, ..."""
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", service)]},
        "scopeSpans": [{"scope": {"name": "sem7.agents"}, "spans": [{
            "traceId": s["trace_id"], "spanId": s["span_id"], **({"parentSpanId": s["parent_id"]}
                                                                  if s.get("parent_id") else {}),
            "name": s["name"], "kind": 1,  # SPAN_KIND_INTERNAL
            "startTimeUnixNano": str(int(s["start"] * 1e9)), "endTimeUnixNano": str(int(s["end"] * 1e9)),
            "attributes": [_attr("sem7.kind", s["kind"]), _attr("sem7.framework", s.get("framework") or "")]
                          + [_attr(f"sem7.{k}", v) for k, v in (s.get("attributes") or {}).items()],
            "status": {"code": 2 if s["status"] == "error" else 1},
        } for s in spans]}],
    }]}
