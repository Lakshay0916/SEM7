"""Generate the Grafana dashboards (JSON) from the OpenTelemetry metrics and traces.

    python monitoring/grafana/build_dashboards.py      # rewrites monitoring/grafana/dashboards/*.json

Metric names are as exported by the Collector (add_metric_suffixes: false): sem7_llm_tokens, sem7_agent_duration_bucket,
http_server_request_duration_bucket, system_cpu_utilization, container_cpu_utilization, ...
Totals are the running total since the service started (counters are cumulative); rates come from rate().
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("dashboards")
PROM = {"type": "prometheus", "uid": "prometheus"}
TEMPO = {"type": "tempo", "uid": "tempo"}
SINCE = "since the service started"


class Board:
    def __init__(self, uid: str, title: str, description: str):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0

    def _place(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title: str) -> None:
        self.x, self.y = 0, self.y + self.row_h
        self.row_h = 0
        self.panels.append({"type": "row", "title": title, "collapsed": False, "panels": [],
                            "gridPos": self._place(24, 1)})
        self.x, self.y, self.row_h = 0, self.y + 1, 0

    def add(self, panel: dict, w: int, h: int) -> None:
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = self._place(w, h)
        self.panels.append(panel)

    def stat(self, title: str, expr: str, unit: str = "short", decimals: int | None = None,
             description: str = "", w: int = 6) -> None:
        defaults = {"unit": unit, "color": {"mode": "fixed", "fixedColor": "blue"}}
        if decimals is not None:
            defaults["decimals"] = decimals
        self.add({"type": "stat", "title": title, "description": description, "datasource": PROM,
                  "targets": [{"refId": "A", "expr": expr, "instant": True}],
                  "fieldConfig": {"defaults": defaults, "overrides": []},
                  "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value",
                              "graphMode": "none", "textMode": "value"}}, w, 4)

    def series(self, title: str, targets: list[tuple[str, str]], unit: str = "short", description: str = "",
               w: int = 12, h: int = 8, stack: bool = False) -> None:
        self.add({"type": "timeseries", "title": title, "description": description, "datasource": PROM,
                  "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": legend}
                              for i, (e, legend) in enumerate(targets)],
                  "fieldConfig": {"defaults": {"unit": unit, "custom": {
                      "lineWidth": 2, "fillOpacity": 10 if not stack else 60, "showPoints": "never",
                      "stacking": {"mode": "normal" if stack else "none"}}}, "overrides": []},
                  "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                              "tooltip": {"mode": "multi", "sort": "desc"}}}, w, h)

    def bars(self, title: str, expr: str, legend: str, unit: str = "short", description: str = "",
             w: int = 12, h: int = 8, decimals: int | None = None) -> None:
        defaults = {"unit": unit, "color": {"mode": "fixed", "fixedColor": "blue"}, "min": 0}
        if decimals is not None:
            defaults["decimals"] = decimals
        self.add({"type": "bargauge", "title": title, "description": description, "datasource": PROM,
                  "targets": [{"refId": "A", "expr": expr, "legendFormat": legend, "instant": True}],
                  "fieldConfig": {"defaults": defaults, "overrides": []},
                  "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                              "valueMode": "text", "reduceOptions": {"calcs": ["lastNotNull"]}}}, w, h)

    def traces(self, title: str, traceql: str, description: str = "", w: int = 24, h: int = 9) -> None:
        self.add({"type": "table", "title": title, "description": description, "datasource": TEMPO,
                  "targets": [{"refId": "A", "queryType": "traceql", "query": traceql, "limit": 20,
                               "tableType": "traces"}]}, w, h)

    def service_map(self, title: str, w: int = 24, h: int = 10) -> None:
        self.add({"type": "nodeGraph", "title": title, "datasource": TEMPO,
                  "description": "Built by Tempo from the traces: which service calls which, how often and how fast.",
                  "targets": [{"refId": "A", "queryType": "serviceMap"}]}, w, h)

    def json(self) -> dict:
        return {"uid": self.uid, "title": self.title, "description": self.description, "tags": ["sem7"],
                "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True, "refresh": "30s",
                "time": {"from": "now-6h", "to": "now"}, "panels": self.panels, "templating": {"list": []},
                "annotations": {"list": []}}


def agents() -> Board:
    b = Board("sem7-agents", "SEM7 · Agents & LLM",
              "The ADK and CrewAI agents: LLM calls, tokens, cost, agent time, MCP tools and A2A hops (OpenTelemetry).")
    b.row("Totals " + SINCE)
    b.stat("LLM calls", "sum(sem7_llm_calls)")
    b.stat("Tokens", "sum(sem7_llm_tokens)")
    b.stat("Cost at list price", "sum(sem7_llm_cost)", "currencyUSD", 4)
    b.stat("Failed LLM calls", "100 * sum(sem7_llm_errors or vector(0)) / clamp_min(sum(sem7_llm_calls), 1)",
           "percent", 1, "Mostly the free tier's rate limit (429).")
    b.row("Per agent")
    b.bars("Tokens by agent", "sum by (agent) (sem7_llm_tokens)", "{{agent}}")
    b.bars("Cost by agent (USD)", "sum by (agent) (sem7_llm_cost)", "{{agent}}", "currencyUSD", decimals=4)
    b.series("Agent run time, p95 by agent (" + SINCE + ")",
             [("histogram_quantile(0.95, sum by (le, agent) (sem7_agent_duration_bucket))", "{{agent}}")],
             "s", "Seconds per agent run (from the ADK / CrewAI callbacks). Over every run since the service "
                  "started, so it shows even when runs are occasional.")
    b.series("LLM calls per minute by agent", [("60 * sum by (agent) (rate(sem7_llm_calls[5m]))", "{{agent}}")])
    b.row("Tools and agent-to-agent")
    b.bars("MCP tool calls", "sum by (tool, status) (sem7_tool_calls)", "{{tool}} · {{status}}")
    b.series("A2A hop latency, p95 (" + SINCE + ")",
             [("histogram_quantile(0.95, sum by (le, service, skill) (sem7_a2a_duration_bucket))",
               "{{service}}/{{skill}}")], "s")
    b.row("Traces")
    b.traces("Latest agent attempts (click a trace for the full waterfall)", '{name=~"repair attempt.*"}',
             "Each repair attempt: input guardrail, agents, every LLM call with tokens, MCP tools, A2A hops.")
    return b


def service() -> Board:
    b = Board("sem7-service", "SEM7 · Service & workflows",
              "The API and the self-healing workflows: requests, latency, outcomes, guardrails, service map.")
    b.row("Workflows " + SINCE)
    b.stat("Workflows finished", "sum(sem7_workflows)")
    b.stat("Fixed (PR created)", "100 * sum(sem7_workflows{outcome=\"pr_created\"} or vector(0)) / "
                                 "clamp_min(sum(sem7_workflows), 1)", "percent", 0)
    b.stat("Avg repair attempts", "sum(sem7_repair_attempts_sum) / clamp_min(sum(sem7_repair_attempts_count), 1)",
           "short", 2)
    b.stat("Guardrail blocks", "sum(sem7_guardrail_verdicts{action=\"block\"} or vector(0))")
    b.bars("Workflows by outcome, strategy and pattern", "sum by (outcome, strategy, pattern) (sem7_workflows)",
           "{{outcome}} · {{strategy}} · {{pattern}}")
    b.bars("Input-guardrail verdicts", "sum by (context, action) (sem7_guardrail_verdicts)", "{{context}} · {{action}}")
    b.row("API (FastAPI instrumentation)")
    b.series("Requests per second by route",
             [("sum by (http_route) (rate(http_server_request_duration_count{service_name=\"sem7-api\"}[5m]))",
               "{{http_route}}")], "reqps")
    b.series("Latency p95 by route",
             [("histogram_quantile(0.95, sum by (le, http_route) "
               "(rate(http_server_request_duration_bucket{service_name=\"sem7-api\"}[5m])))", "{{http_route}}")], "s")
    b.series("Server errors (5xx) per second",
             [("sum(rate(http_server_request_duration_count{service_name=\"sem7-api\", "
               "http_response_status_code=~\"5..\"}[5m])) or vector(0)", "5xx")], "reqps", w=24, h=6)
    b.row("Service map and slow requests")
    b.service_map("Service map (api → agent services)")
    b.traces("Slowest API requests (over 5 s)", '{resource.service.name="sem7-api" && duration > 5s}')
    return b


def infrastructure() -> Board:
    b = Board("sem7-infra", "SEM7 · Infrastructure",
              "The EC2 host and every container (OpenTelemetry Collector host metrics and Docker stats).")
    b.row("Host now")
    b.stat("CPU used", "100 * (1 - avg(system_cpu_utilization{state=\"idle\"}))", "percent", 0)
    b.stat("Memory used", "100 * sum(system_memory_utilization{state=\"used\"})", "percent", 0)
    b.stat("Disk used (/)", "100 * max(system_filesystem_utilization{mountpoint=\"/\"})", "percent", 0)
    b.stat("Load (1 min)", "max(system_cpu_load_average_1m)", "short", 2)
    b.row("Host over time")
    b.series("CPU by state", [("100 * avg by (state) (system_cpu_utilization{state!=\"idle\"})", "{{state}}")],
             "percent", stack=True)
    b.series("Memory", [("sum by (state) (system_memory_usage{state=~\"used|cached|buffered\"})", "{{state}}")],
             "bytes", stack=True)
    b.row("Containers")
    b.series("CPU by container", [("sum by (container_name) (container_cpu_utilization)", "{{container_name}}")],
             "percent")
    b.series("Memory by container", [("sum by (container_name) (container_memory_usage_total)", "{{container_name}}")],
             "bytes")
    b.series("Network", [("sum by (direction) (rate(system_network_io[5m]))", "{{direction}}")], "Bps", w=24, h=6)
    return b


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for board in (agents(), service(), infrastructure()):
        (OUT / f"{board.uid}.json").write_text(json.dumps(board.json(), indent=1) + "\n")
        print(f"wrote {board.uid}.json ({len(board.panels)} panels)")
