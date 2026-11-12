import pandas as pd
import streamlit as st

from ui.api_client import get_api, require_api
from ui.observability import metric_heatmap, run_heatmap
from ui.style import hero, kv_tiles

METRICS = {"avg_seconds": "Avg time (s)", "avg_tokens": "Avg tokens", "avg_llm_calls": "LLM calls / run",
           "avg_tool_calls": "Tool calls / run", "errors": "Errors"}
FRAMEWORK = {"adk": "🔷 ADK", "crewai": "🟧 CrewAI"}


def render() -> None:
    st.title("Agent Monitoring")
    hero(
        "Where the agents spend time, tokens and tool calls",
        "Every AI-agent attempt is traced: ADK agent and model callbacks, ADK tool callbacks and CrewAI's event "
        "bus turn each agent run, LLM call, MCP tool call, delegation and A2A hop into a timed span. This page "
        "adds them up per agent across all traced runs; open a workflow's attempt to see its full trace.",
        chips=["📞 Callbacks", "🧵 Traces (OpenTelemetry model)", "🔥 Heat maps", "📤 OTLP export"],
    )
    require_api()
    data = get_api().agent_monitoring()
    runs, agents, cells = (pd.DataFrame(data[k]) for k in ("runs", "agents", "cells"))
    if runs.empty:
        st.info("No traced runs yet. Run a scenario with **🤖 AI agents** on the Run Pipeline page - each "
                "attempt's trace appears here.")
        return

    kv_tiles({"Traced attempts": str(len(runs)), "Agent runs": str(int(agents["runs"].sum())),
              "LLM calls": f"{int(runs['llm_calls'].sum()):,}", "Tokens": f"{int(runs['tokens'].sum()):,}",
              "MCP tool calls": f"{int(runs['tool_calls'].sum()):,}",
              "Avg attempt": f"{runs['seconds'].mean():.0f} s"})

    st.markdown("#### Heat map: each agent at a glance")
    st.caption("Averages per agent run. Colour is relative within each column (the columns have different "
               "units); the number is the real value. Hover a cell for details.")
    agents["Agent"] = agents.apply(lambda r: f"{FRAMEWORK.get(r['framework'], r['framework'])} · {r['agent']}",
                                   axis=1)
    st.altair_chart(metric_heatmap(agents, METRICS), width="stretch")

    st.markdown("#### Heat map: every agent in every traced attempt")
    by = st.radio("Colour by", ["seconds", "tokens", "llm_calls"], horizontal=True, key="mon_by",
                  format_func={"seconds": "time", "tokens": "tokens", "llm_calls": "LLM calls"}.get)
    runs["column"] = runs["pattern"].fillna("?") + " · " + runs["label"]  # runs are oldest first
    cells = cells.merge(runs[["label", "pattern", "column"]], left_on="run", right_on="label", how="left")
    cells["run"] = cells["column"]
    title, fmt = {"seconds": ("seconds", ".1f"), "tokens": ("tokens", ","), "llm_calls": ("LLM calls", "d")}[by]
    st.altair_chart(run_heatmap(cells, by, title, fmt, runs["column"].tolist()), width="stretch")
    st.caption("Empty cells: that agent did not run in that attempt (each pattern uses different agents - e.g. "
               "the parallel specialists or the hierarchical Repair Lead).")

    st.markdown("#### By pattern")
    by_pattern = runs.groupby("pattern", dropna=False).agg(
        attempts=("label", "count"), avg_seconds=("seconds", "mean"), avg_tokens=("tokens", "mean"),
        avg_llm_calls=("llm_calls", "mean"), avg_tool_calls=("tool_calls", "mean"), errors=("errors", "sum"),
    ).reset_index().round(1)
    st.dataframe(by_pattern, hide_index=True, width="stretch")

    with st.expander("Table view: every agent (all numbers)"):
        st.dataframe(agents.drop(columns=["Agent"]), hide_index=True, width="stretch")
    with st.expander("Table view: every traced attempt"):
        st.dataframe(runs.drop(columns=["started"]), hide_index=True, width="stretch")
