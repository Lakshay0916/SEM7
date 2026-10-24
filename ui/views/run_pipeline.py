import streamlit as st

from ui.api_client import APIError, get_api, require_api
from ui.style import hero
from ui.workflow_view import render_workflow

SCENARIO_HINT = {
    "simple_bug": "Expect: tests **fail** (2 of 9); one minimal fix (restore `+`) repairs it after your approval.",
    "high_risk_change": "Expect: tests **pass** but the change is broad - shows that risk ≠ bug.",
    "failed_first_repair": "Expect: tests **fail** (3 of 9) from two defects. Rule-based: fix 1 only partly works → "
                           "reflection → fix 2 → tests pass → PR. AI agents usually fix both defects in one attempt.",
}


STRATEGY_HELP = {
    "rules": "Deterministic Python rules investigate the failure and propose a fix. Instant, no API key, "
             "same result every time - this is what CI uses.",
    "agents": "Four LLM agents on Gemini: **Google ADK** Investigator → Root-cause analyst, then a **CrewAI** "
              "Planner → Reviewer crew. They see only the real evidence; their plan still goes through the safety "
              "checks, your approval and the real tests. Each agent step (time, tokens) appears in the timeline.",
}


MESSAGING_HELP = {
    "blackboard": "All agents share one **blackboard**: each reads what it needs from the board and posts its "
                  "result there; a controller picks who runs next from what is on the board. Guardrail feedback "
                  "is posted to the board too. The whole board is saved with the workflow.",
    "direct": "**Direct hand-off**: each agent passes its output straight to the next one (ADK session state, then "
              "CrewAI task context). No shared board - kept to compare against the blackboard.",
}


def _strategy_picker(api) -> tuple[str, str | None, bool]:
    """Rule-based vs AI-agent analysis (+ how agents share context). Returns (strategy, messaging, can run)."""
    options = {o["id"]: o for o in api.strategies()}
    default = next((o["id"] for o in options.values() if o["default"]), "rules")
    choice = st.segmented_control(
        "2. Who analyses a failure and proposes the fix?", list(options), default=default, key="strategy", required=True,
        format_func=lambda i: ("⚙️ " if i == "rules" else "🤖 ") + options[i]["label"], width="stretch",
    )
    opt = options[choice]
    st.caption(STRATEGY_HELP[choice] + (f" Model: `{opt['model']}`." if opt.get("model") else ""))
    if not opt["available"]:
        st.warning(f"**{opt['label']} unavailable on this backend:** {opt['reason']}.")
    messaging = None
    if choice == "agents" and opt["available"] and opt.get("messaging"):
        modes = {m["id"]: m for m in opt["messaging"]}
        messaging = st.radio("How do the agents share context?", list(modes), horizontal=True, key="messaging",
                             index=next((i for i, m in enumerate(modes.values()) if m["default"]), 0),
                             format_func=lambda m: ("🧩 " if m == "blackboard" else "🔗 ") + modes[m]["label"])
        st.caption(MESSAGING_HELP[messaging])
    return choice, messaging, opt["available"]


def render() -> None:
    st.title("Run Pipeline")
    hero(
        "Break a project on purpose, then watch the pipeline react",
        "Choose a scenario and press Run. The backend creates a fresh git repository from a small calculator "
        "project, commits the scenario's change on a feature branch, then analyses, risk-scores and really tests "
        "it. Each step below explains what it did and shows its actual output.",
        chips=["1 · Pick a scenario", "2 · Rules or AI agents", "3 · Run", "4 · Read the step-by-step report"],
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

    strategy, messaging, ready = _strategy_picker(api)
    if st.button("3. ▶️ Run the pipeline", type="primary", width="stretch", disabled=not ready):
        label = "AI agents are analysing the failure (about 20-40 s)…" if strategy == "agents" else \
            "Backend is running the pipeline…"
        with st.status(label, expanded=True) as status:
            try:
                res = api.run(scenario_id, strategy=strategy, messaging=messaging)
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
