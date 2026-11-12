import pandas as pd
import streamlit as st

from ui.api_client import get_api, require_api
from ui.common import boxplot
from ui.style import hero, kv_tiles

PATTERNS = ["sequential", "parallel", "hierarchical"]
MEASURES = {
    "analysis_seconds": ("Analysis time (s)", ",.1f"), "tokens": ("Tokens per run", ",.0f"),
    "cost_usd": ("Cost per run (USD, list price)", "$.4f"), "llm_calls": ("LLM calls per run", ",.0f"),
    "wall_seconds": ("Whole loop incl. repair + tests (s)", ",.1f"),
}


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def render() -> None:
    st.title("Evaluation")
    hero(
        "How the agent configurations compare over many real runs",
        "Each run is the whole self-healing loop on a fresh commit: real tests, analysis, approval (given "
        "automatically for the evaluation), repair, real re-test, reflection and a PR. Time, tokens and LLM calls "
        "come from the saved traces; cost is tokens x Gemini 3.1 Flash-Lite's list price. Produced by "
        "`scripts/run_evaluation.py`.",
        chips=["🔁 Full loop per run", "📦 Boxplots", "💲 Cost", "⚖️ Rules baseline"],
    )
    require_api()
    data = get_api().agent_evaluation()
    runs = pd.DataFrame(data["runs"])
    if runs.empty:
        st.info("No evaluation yet. Run `python scripts/run_evaluation.py` (SEM7-agents environment).")
        return
    s = data["summary"]
    t = s["totals"]
    kv_tiles({"Runs": str(s["runs"]), "LLM calls": f"{t['llm_calls']:,}", "Tokens": f"{t['tokens']:,}",
              "Cost (list price)": f"${t['cost_usd']:.3f}", "Run time": f"{t['wall_hours']} h"})

    st.markdown("#### Engines")
    st.dataframe(pd.DataFrame([{
        "Group": g, "Runs": v["runs"], "Fixed": _pct(v["success_rate"]),
        "Fixed by agents alone": _pct(v["agent_success_rate"]), "First try": _pct(v["first_try_rate"]),
        "Root cause names the function": _pct(v["root_cause_correct_rate"]),
        "Median analysis (s)": v["analysis_seconds"]["median"], "Median tokens": v["tokens"]["median"],
        "Cost": f"${v['total_cost_usd']:.4f}"} for g, v in s["groups"].items()]), hide_index=True, width="stretch")
    st.caption("rules = deterministic baseline (no LLM) · guardrail = scenario 4 with AI agents, where the input "
               "guardrail blocks the injected commit before any LLM call · agents = the 12 configurations below.")

    agents = runs[runs["group"] == "agents"].copy()
    if agents.empty:
        return
    agents["configuration"] = agents["pattern"] + " · " + agents["messaging"]
    order = [f"{p} · {m}" for p in PATTERNS for m in ("blackboard", "direct")]
    st.markdown("#### Boxplots: agent configurations")
    measure = st.radio("Measure", list(MEASURES), horizontal=True, key="eval_measure",
                       format_func=lambda m: MEASURES[m][0].split(" (")[0])
    title, fmt = MEASURES[measure]
    st.altair_chart(boxplot(agents, "configuration", measure, "pattern", order, PATTERNS, title, fmt,
                            ["run_id:N", "scenario:N", "final_state:N", "attempts:Q"],
                            log=measure.endswith("seconds")), width="stretch")
    st.caption("Box = middle half of the runs (quartiles), line = median, whiskers = min and max; each dot is one "
               "run (both scenarios). With ~6 runs per box, overlapping boxes mean no clear difference. Times are on "
               "a log scale: a few runs waited minutes on the free tier's rate limit.")

    st.markdown("#### Outcomes per configuration")
    st.dataframe(pd.DataFrame([{
        "Configuration": c, "Runs": v["runs"], "Fixed": _pct(v["success_rate"]),
        "Fixed by agents alone": _pct(v["agent_success_rate"]), "First try": _pct(v["first_try_rate"]),
        "Root cause names the function": _pct(v["root_cause_correct_rate"]), "Runs with a fallback": v["fallback_runs"],
        "LLM errors (mostly rate limits)": v["llm_errors"]} for c, v in s["configurations"].items()]),
        hide_index=True, width="stretch")
    with st.expander("Table view: every run"):
        st.dataframe(runs.drop(columns=["daily_quota_hit"], errors="ignore"), hide_index=True, width="stretch")
