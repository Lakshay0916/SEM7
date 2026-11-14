"""OpenTelemetry metrics for the self-healing system (no-ops until app/observability/otel.py sets them up).

What the Grafana dashboards are built from:
    sem7.workflows                  workflows that finished, by outcome / strategy / pattern / scenario
    sem7.repair.attempts            repair attempts a finished workflow needed (histogram)
    sem7.agent.duration             seconds per agent run, by agent / framework / pattern (histogram)
    sem7.llm.calls / llm.errors     LLM calls (and failed ones), by agent / model
    sem7.llm.tokens                 tokens, by agent / model / type (input | output)
    sem7.llm.cost                   USD at the model's list price, by agent / model
    sem7.tool.calls                 MCP tool calls, by tool / status
    sem7.a2a.duration               seconds per A2A hop, by service / skill (histogram)
    sem7.guardrail.verdicts         input-guardrail decisions, by context (chat | evidence) / action
    sem7.notifications              notifications sent (or failed), by channel / event / status
HTTP request rates and latencies come from the FastAPI instrumentation.
"""

from __future__ import annotations

from functools import lru_cache

# Gemini 3.1 Flash-Lite list price, standard tier, USD per million tokens (ai.google.dev, October 2026)
PRICE_PER_MTOK = {"input": 0.25, "output": 1.50}


@lru_cache(maxsize=1)
def _instruments() -> dict:
    from opentelemetry import metrics

    m = metrics.get_meter("sem7")
    return {
        "workflows": m.create_counter("sem7.workflows", description="Workflows that finished, by outcome"),
        "attempts": m.create_histogram("sem7.repair.attempts", description="Repair attempts per finished workflow"),
        "agent_duration": m.create_histogram("sem7.agent.duration", unit="s", description="Seconds per agent run"),
        "llm_calls": m.create_counter("sem7.llm.calls", description="LLM calls"),
        "llm_errors": m.create_counter("sem7.llm.errors", description="Failed LLM calls (mostly rate limits)"),
        "tokens": m.create_counter("sem7.llm.tokens", unit="{token}", description="LLM tokens by type"),
        "cost": m.create_counter("sem7.llm.cost", unit="USD", description="LLM cost at list price"),
        "tool_calls": m.create_counter("sem7.tool.calls", description="MCP tool calls by status"),
        "a2a_duration": m.create_histogram("sem7.a2a.duration", unit="s", description="Seconds per A2A hop"),
        "guardrail": m.create_counter("sem7.guardrail.verdicts", description="Input-guardrail decisions"),
        "notifications": m.create_counter("sem7.notifications", description="Notifications sent, by status"),
    }


def reset() -> None:
    """Forget the instruments (tests that install a fresh MeterProvider call this)."""
    _instruments.cache_clear()


def _owner(span: dict, by_id: dict) -> dict | None:
    s = span
    while s is not None:
        if s["kind"] == "agent":
            return s
        s = by_id.get(s.get("parent_id"))
    return None


def record_attempt(spans: list[dict]) -> None:
    """Metrics from one finished agent attempt's trace (agent time, LLM calls / tokens / cost, tools, A2A)."""
    i = _instruments()
    by_id = {s["span_id"]: s for s in spans}
    root = next((s for s in spans if s["kind"] == "attempt"), None)
    pattern = ((root or {}).get("attributes") or {}).get("pattern") or "sequential"
    for s in spans:
        a = s.get("attributes") or {}
        if s["kind"] == "agent":
            i["agent_duration"].record(s["seconds"], {"agent": s["name"], "framework": s.get("framework") or "",
                                                      "pattern": pattern})
        elif s["kind"] == "llm":
            owner = _owner(s, by_id)
            labels = {"agent": owner["name"] if owner else "unknown", "model": str(a.get("model") or "unknown")}
            i["llm_calls"].add(1, labels)
            if s["status"] == "error":
                i["llm_errors"].add(1, labels)
            prompt, output = a.get("prompt_tokens") or 0, a.get("output_tokens") or 0
            i["tokens"].add(prompt, {**labels, "type": "input"})
            i["tokens"].add(output, {**labels, "type": "output"})
            i["cost"].add(prompt / 1e6 * PRICE_PER_MTOK["input"] + output / 1e6 * PRICE_PER_MTOK["output"], labels)
        elif s["kind"] == "tool":
            i["tool_calls"].add(1, {"tool": s["name"].split("· ")[-1], "status": s["status"]})
        elif s["kind"] == "a2a" and s["name"].startswith("A2A → "):
            service, _, skill = s["name"].removeprefix("A2A → ").partition("/")
            i["a2a_duration"].record(s["seconds"], {"service": service, "skill": skill})


def record_workflow(outcome: str, attempts: int, strategy: str | None, pattern: str | None,
                    scenario: str | None) -> None:
    i = _instruments()
    labels = {"outcome": outcome, "strategy": strategy or "rules", "pattern": pattern or "none",
              "scenario": scenario or "unknown"}
    i["workflows"].add(1, labels)
    i["attempts"].record(attempts, labels)


def record_guardrail(context: str, action: str) -> None:
    _instruments()["guardrail"].add(1, {"context": context, "action": action})


def record_notification(channel: str, event: str, status: str) -> None:
    _instruments()["notifications"].add(1, {"channel": channel, "event": event, "status": status})
