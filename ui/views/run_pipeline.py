import streamlit as st

from app.ci.scenarios import SCENARIOS
from app.orchestration.pipeline import run_ci_workflow
from ui.common import get_predictor, get_store
from ui.style import hero
from ui.workflow_view import render_workflow, strategy

STEP_LABEL = {"sandbox": "1 · Commit", "diff": "2 · Diff", "risk": "3 · Risk", "tests": "4 · Tests",
              "heal": "6 · Self-healing"}

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
        "Choose a scenario and press Run. A fresh git repository is created from a small calculator project, "
        "the scenario's change is committed on a feature branch, and the pipeline analyses, risk-scores and "
        "really tests it. Each step below explains what it did and shows its actual output.",
        chips=["1 · Pick a scenario", "2 · Run", "3 · Read the step-by-step report"],
    )

    left, right = st.columns([2, 3])
    with left:
        scenario_id = st.radio(
            "1. Choose a scenario", list(SCENARIOS), format_func=lambda s: SCENARIOS[s].title,
        )
    sc = SCENARIOS[scenario_id]
    with right:
        with st.container(border=True):
            st.markdown(f"**{sc.title}**")
            st.write(sc.description)
            st.markdown(SCENARIO_HINT.get(scenario_id, ""))
            with st.expander("See the exact code change this scenario commits"):
                st.markdown(f"Branch `{sc.branch}` · commit message _{sc.commit_message}_")
                for e in sc.edits:
                    st.markdown(f"`{e.file}`" + (" (new file)" if not e.old else ""))
                    if e.old:
                        st.code("".join(f"-{l}\n" for l in e.old.splitlines())
                                + "".join(f"+{l}\n" for l in e.new.splitlines()), language="diff")
                    else:
                        st.code(e.new, language="python")

    predictor = get_predictor()
    if predictor is None:
        st.warning("No trained risk model found - step 3 will be skipped. Run `python scripts/train_risk_model.py`.")

    if st.button("2. ▶️ Run the pipeline", type="primary", width="stretch"):
        with st.status("Running pipeline…", expanded=True) as status:
            def on_step(step: str, detail: str) -> None:
                st.write(f"**{STEP_LABEL.get(step, step)}** — {detail}")

            result = run_ci_workflow(scenario_id, get_store(), predictor, on_step=on_step,
                                     strategy=strategy())
            st.write(f"**7 · Record** — saved as workflow `{result.workflow_id}`")
            verdict = result.run.test_report.status.value
            status.update(
                label=(f"Tests {verdict} - a fix has been proposed. Scroll down to review and approve it."
                       if verdict != "PASS" else "Tests PASS - scroll down for the step-by-step explanation."),
                state="error" if verdict != "PASS" else "complete", expanded=False,
            )
        st.session_state["current_wf"] = result.workflow_id

    if wf_id := st.session_state.get("current_wf"):
        st.divider()
        render_workflow(wf_id)
