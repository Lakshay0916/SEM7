import pandas as pd
import streamlit as st

from ui.common import get_store
from ui.workflow_view import render_workflow


def render() -> None:
    st.title("Workflow History")
    st.write(
        "Every run is saved in a SQLite database (`workspace/sem7.sqlite3`): each state change, which "
        "component did it, and every output it produced. Pick any past run to replay its step-by-step report."
    )
    store = get_store()
    wfs = store.list_workflows(limit=500)
    if not wfs:
        st.info("No workflows yet - run one from **Run Pipeline**.")
        return

    states = sorted({w["state"] for w in wfs})
    chosen = st.multiselect("Filter by final state", states, default=states,
                            help="FAILED = tests failed (hand-off to agents) · PASSED = nothing to repair")
    wfs = [w for w in wfs if w["state"] in chosen]
    if not wfs:
        return
    df = pd.DataFrame(wfs)[["id", "scenario", "state", "branch", "created_at"]]
    df["created_at"] = df["created_at"].str[:19].str.replace("T", " ")
    st.dataframe(df, hide_index=True, width="stretch")

    wf_id = st.selectbox(
        "Open a workflow", [w["id"] for w in wfs],
        format_func=lambda i: next(f"{w['id']} · {w['scenario']} · {w['state']} · {w['created_at'][:19]}"
                                   for w in wfs if w["id"] == i),
    )
    st.divider()
    render_workflow(wf_id)
