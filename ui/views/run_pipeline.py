import streamlit as st

from ui.api_client import APIError, get_api, require_api
from ui.style import hero
from ui.workflow_view import render_workflow

SCENARIO_HINT = {
    "simple_bug": "Expect: tests **fail** (2 of 9); one minimal fix (restore `+`) repairs it after your approval.",
    "high_risk_change": "Expect: tests **pass** but the change is broad - shows that risk ≠ bug.",
    "failed_first_repair": "Expect: tests **fail** (3 of 9) from two defects. Fix 1 only partly works → "
                           "reflection → fix 2 → tests pass → PR.",
}


def render() -> None:
    st.title("Run Pipeline")
    hero(
        "Break a project on purpose, then watch the pipeline react",
        "Choose a scenario and press Run. The backend creates a fresh git repository from a small calculator "
        "project, commits the scenario's change on a feature branch, then analyses, risk-scores and really tests "
        "it. Each step below explains what it did and shows its actual output.",
        chips=["1 · Pick a scenario", "2 · Run", "3 · Read the step-by-step report"],
    )
    require_api()
    api = get_api()
    scenarios = {s["id"]: s for s in api.scenarios()}

    left, right = st.columns([2, 3])
    with left:
        scenario_id = st.radio("1. Choose a scenario", list(scenarios), format_func=lambda s: scenarios[s]["title"])
    sc = scenarios[scenario_id]
    with right:
        with st.container(border=True):
            st.markdown(f"**{sc['title']}**")
            st.write(sc["description"])
            st.markdown(SCENARIO_HINT.get(scenario_id, ""))
            with st.expander("See the exact code change this scenario commits"):
                st.markdown(f"Branch `{sc['branch']}` · commit message _{sc['commit_message']}_")
                for e in sc["edits"]:
                    st.markdown(f"`{e['file']}`" + (" (new file)" if not e["old"] else ""))
                    if e["old"]:
                        st.code("".join(f"-{l}\n" for l in e["old"].splitlines())
                                + "".join(f"+{l}\n" for l in e["new"].splitlines()), language="diff")
                    else:
                        st.code(e["new"], language="python")

    if st.button("2. ▶️ Run the pipeline", type="primary", width="stretch"):
        with st.status("Backend is running the pipeline…", expanded=True) as status:
            try:
                res = api.run(scenario_id)
            except APIError as exc:
                status.update(label=f"Failed: {exc.detail}", state="error")
                st.stop()
            for e in api.trace(res["workflow_id"])["events"]:
                st.write(f"**{e['agent']}** — {e['message']}")
            waiting = res["state"] == "WAITING_APPROVAL"
            status.update(
                label=("Tests failed - a fix has been proposed. Scroll down to review and approve it." if waiting
                       else f"Finished in state {res['state']} - scroll down for the step-by-step explanation."),
                state="error" if waiting else "complete", expanded=False,
            )
        st.session_state["current_wf"] = res["workflow_id"]

    if wf_id := st.session_state.get("current_wf"):
        st.divider()
        render_workflow(wf_id)
