import pandas as pd
import streamlit as st

from ui.common import IMPLEMENTED_PHASE, get_predictor, get_store, load_json
from ui.content import PROGRESS
from ui.style import hero


def render() -> None:
    st.title("AI Self-Healing CI/CD")
    hero(
        "From a broken commit to a verified fix - with a human in control",
        "When a commit breaks the tests, this system predicts how risky the change was, captures the real "
        "test evidence, and (in upcoming phases) uses AI agents and project knowledge to find the root cause, "
        "propose a fix for your approval, apply it on a separate branch and prove it with real tests before "
        "opening a pull request.",
        "ML predicts · RAG retrieves · LLM reasons · agents act · tests verify",
    )

    store = get_store()
    wfs = store.list_workflows(limit=500)
    predictor = get_predictor()
    evaluation = load_json("evaluation.json")

    cols = st.columns(4)
    cols[0].metric("Workflows run", len(wfs), help="Each click of 'Run' on the Run Pipeline page")
    cols[1].metric("CI failures caught", sum(w["state"] == "FAILED" for w in wfs),
                   help="Runs where the real tests failed and a FailureEvent was produced")
    cols[2].metric("Passed CI", sum(w["state"] == "PASSED" for w in wfs))
    if evaluation:
        full = evaluation["variants"]["full"]
        test = full["candidates"][full["chosen_model"]]["test"]
        cols[3].metric("Risk model ROC-AUC", f"{test['roc_auc']:.3f}",
                       help="'full' model on held-out newest commits; 0.5 = random, 1.0 = perfect")
    else:
        cols[3].metric("Risk model", "not trained")

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
        st.caption("Real metrics on 3,625 open-source commits, compared with baselines.")
        _link("risk", "Open Risk Model", "📈")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Recent workflows")
        if wfs:
            df = pd.DataFrame(wfs)[["id", "scenario", "state", "branch", "created_at"]]
            df["created_at"] = df["created_at"].str[:19].str.replace("T", " ")
            st.dataframe(df.head(12), hide_index=True, width="stretch")
        else:
            st.info("No workflows yet - open **Run Pipeline** to run a demo scenario.")
    with right:
        st.markdown("#### Build progress")
        st.dataframe(
            pd.DataFrame([
                {"Stage": label, "Phase": phase,
                 "Status": "✅ Built" if phase <= IMPLEMENTED_PHASE else "⏳ Planned"}
                for _, label, phase in PROGRESS
            ]),
            hide_index=True, width="stretch",
        )
        st.caption(f"Risk model version `{predictor.version}`" if predictor else "Risk model not trained.")


def _link(url_path: str, label: str, icon: str) -> None:
    """Link to another page (page objects are registered by ui/app.py)."""
    page = st.session_state.get("_pages", {}).get(url_path)
    if page is not None:
        st.page_link(page, label=label, icon=icon)
    else:
        st.caption(f"{icon} {label} (use the sidebar)")
