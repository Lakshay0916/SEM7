import pandas as pd
import streamlit as st

from ui.common import get_predictor, get_store, load_json
from ui.content import PROGRESS
from ui.style import hero, tiles


def _link(url_path: str, label: str, icon: str) -> None:
    """Link to another page (page objects are registered by ui/app.py)."""
    page = st.session_state.get("_pages", {}).get(url_path)
    if page is not None:
        st.page_link(page, label=label, icon=icon)
    else:
        st.caption(f"{icon} {label} (use the sidebar)")


def render() -> None:
    st.title("AI Self-Healing CI/CD")
    hero(
        "From a broken commit to a verified fix — with a human in control",
     "When a commit breaks the tests, this system scores how risky the change was, captures the real test "
        "evidence, investigates the root cause and proposes a fix. After your approval it repairs on a separate "
        "branch, proves the fix with real tests (reflecting and retrying if needed) and opens a pull request. "
        "Analysis is rule-based today; LLM agents and RAG plug into the same loop next.",
        chips=["🔁 Self-healing loop with human approval", "🧪 Real tests, never faked", "🛡️ AI can't touch main",
               "🚦 Risk-aware quality gate", "🐳 Docker + GitHub Actions"],
    )

    store = get_store()
    wfs = store.list_workflows(limit=500)
    predictor = get_predictor()
    evaluation = load_json("evaluation.json")

    cols = st.columns(4)
    cols[0].metric("Workflows run", len(wfs), help="Each click of 'Run' on the Run Pipeline page")
    cols[1].metric("CI failures caught", sum(w["state"] == "FAILED" for w in wfs),
                   help="Runs where the real tests failed and a FailureEvent was produced")
    cols[2].metric("Fixes validated → PR", sum(w["state"] == "PR_CREATED" for w in wfs),
                   help="Repairs that passed the real test suite and produced a pull request")
    if evaluation:
        full = evaluation["variants"]["full"]
        test = full["candidates"][full["chosen_model"]]["test"]
        cols[3].metric("Risk model ROC-AUC", f"{test['roc_auc']:.3f}",
                       help="'full' model on held-out newest commits; 0.5 = random, 1.0 = perfect")
    else:
        cols[3].metric("Risk model", "not trained")

    st.markdown("#### What's working today")
    tiles([
        ("📈", "ML risk + quality gate", "Risk scored before tests; tests always run; HIGH risk adds a review flag."),
        ("🧪", "Real CI tests", "pytest actually runs; PASS only with real evidence — timeouts and crashes never pass."),
        ("🔁", "Self-healing loop", "Investigate → root cause → plan → your approval → repair branch → re-test → PR."),
        ("🧭", "Full traceability", "Every state change, decision and output saved to SQLite and exportable."),
    ])

    st.markdown("#### Get started")
    g1, g2, g3 = st.columns(3)
    with g1.container(border=True):
        st.markdown("**1 · Understand the system**")
        st.caption("Architecture, what each technology does, and the safety rules.")
        _link("how-it-works", "Open How it works", "📘")
    with g2.container(border=True):
        st.markdown("**2 · Run a scenario**")
        st.caption("Break a demo project on purpose and watch each step explain itself.")
        _link("run", "Open Run Pipeline", "▶️")
    with g3.container(border=True):
        st.markdown("**3 · Inspect the ML model**")
        st.caption("Real metrics on open-source commits, compared with simple baselines.")
        _link("risk", "Open Risk Model", "📈")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Recent workflows")
        if wfs:
            df = pd.DataFrame(wfs)[["id", "scenario", "state", "branch", "created_at"]]
            df["state"] = df["state"].map(lambda s: {"FAILED": "❌ FAILED", "PASSED": "✅ PASSED"}.get(s, s))
            df["created_at"] = df["created_at"].str[:19].str.replace("T", " ")
            st.dataframe(df.head(12), hide_index=True, width="stretch")
        else:
            st.info("No workflows yet — open **Run Pipeline** to run a demo scenario.")
    with right:
        st.markdown("#### Build progress")
        built = sum(status != "planned" for _, _, status, _ in PROGRESS)
        st.progress(built / len(PROGRESS), text=f"{built} of {len(PROGRESS)} pipeline stages working")
        st.dataframe(
            pd.DataFrame([
                {"Stage": label,
                 "Status": {"built": "✅ Built", "rule": "✅ Rule-based (LLM agent next)",
                            "planned": "⏳ Planned"}[status]}
                for _, label, status, _ in PROGRESS
            ]),
            hide_index=True, width="stretch", height=300,
        )
        st.caption(f"Risk model version `{predictor.version}`" if predictor else "Risk model not trained.")
