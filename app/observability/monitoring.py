"""Per-agent monitoring from the saved traces: what the heat maps on the Agent Monitoring page show.

Every agent span (an ADK agent run or a CrewAI agent run) is credited with the LLM calls, tokens and tool
calls made inside it (its nearest agent ancestor), so the numbers come straight from the callbacks.
"""

from __future__ import annotations

from collections import defaultdict

from app.orchestration.store import Store


def _owner(span: dict, by_id: dict[str, dict]) -> dict | None:
    """The agent span a span belongs to (itself if it is one)."""
    s = span
    while s is not None:
        if s["kind"] == "agent":
            return s
        s = by_id.get(s.get("parent_id"))
    return None


def run_summary(spans: list[dict]) -> dict:
    root = next((s for s in spans if s["kind"] == "attempt"), {})
    llm = [s for s in spans if s["kind"] == "llm"]
    tokens = sum((s["attributes"].get("prompt_tokens") or 0) + (s["attributes"].get("output_tokens") or 0)
                 for s in llm)
    return {"seconds": root.get("seconds", 0), "pattern": root.get("attributes", {}).get("pattern"),
            "messaging": root.get("attributes", {}).get("messaging"),
            "transport": root.get("attributes", {}).get("transport"), "spans": len(spans),
            "agents": sum(s["kind"] == "agent" for s in spans), "llm_calls": len(llm), "tokens": tokens,
            "tool_calls": sum(s["kind"] == "tool" for s in spans),
            "errors": sum(s["status"] == "error" for s in spans), "started": root.get("start")}


def agent_runs(spans: list[dict]) -> list[dict]:
    """One row per agent span: its time, and the LLM calls / tokens / tool calls made inside it."""
    by_id = {s["span_id"]: s for s in spans}
    rows: dict[str, dict] = {}
    for s in spans:
        if s["kind"] == "agent":
            rows[s["span_id"]] = {"agent": s["name"], "framework": s.get("framework", ""), "seconds": s["seconds"],
                                  "llm_calls": 0, "tokens": 0, "tool_calls": 0, "tool_errors": 0,
                                  "errors": int(s["status"] == "error")}
    for s in spans:
        owner = _owner(s, by_id) if s["kind"] in ("llm", "tool") else None
        if owner is None or owner["span_id"] not in rows:
            continue
        row = rows[owner["span_id"]]
        if s["kind"] == "llm":
            row["llm_calls"] += 1
            row["tokens"] += (s["attributes"].get("prompt_tokens") or 0) + (s["attributes"].get("output_tokens") or 0)
            row["errors"] += int(s["status"] == "error")
        else:
            row["tool_calls"] += 1
            row["tool_errors"] += int(s["status"] == "error")
    return list(rows.values())


def monitoring(store: Store, limit: int = 300) -> dict:
    """Runs (one per traced attempt), per-agent totals, and agent x run cells for the heat maps."""
    runs, cells = [], []
    totals: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(float))
    for wf in store.list_workflows(limit=limit):
        for art in store.artifacts(wf["id"], "trace"):
            spans = art["payload"]["spans"]
            label = f"{wf['id'][-6:]}·{art['attempt']}"
            runs.append({"workflow_id": wf["id"], "scenario": wf["scenario"], "attempt": art["attempt"],
                         "label": label, **run_summary(spans)})
            for row in agent_runs(spans):
                cells.append({"run": label, "workflow_id": wf["id"], **row})
                t = totals[(row["agent"], row["framework"])]
                t["runs"] += 1
                for k in ("seconds", "llm_calls", "tokens", "tool_calls", "tool_errors", "errors"):
                    t[k] += row[k]
    agents = [{"agent": a, "framework": f, "runs": int(t["runs"]),
               "avg_seconds": round(t["seconds"] / t["runs"], 2), "llm_calls": int(t["llm_calls"]),
               "avg_llm_calls": round(t["llm_calls"] / t["runs"], 2), "tokens": int(t["tokens"]),
               "avg_tokens": int(t["tokens"] / t["runs"]), "tool_calls": int(t["tool_calls"]),
               "avg_tool_calls": round(t["tool_calls"] / t["runs"], 2), "tool_errors": int(t["tool_errors"]),
               "errors": int(t["errors"])} for (a, f), t in totals.items()]
    agents.sort(key=lambda r: (r["framework"], r["agent"]))
    runs.sort(key=lambda r: r["started"] or 0)
    return {"runs": runs, "agents": agents, "cells": cells}
