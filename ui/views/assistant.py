import streamlit as st

from ui.api_client import APIError, get_api, require_api

HISTORY_KEY = "assistant_history"  # [{role, content, sources?, note?, provider?}]
PENDING_KEY = "assistant_pending"  # a suggested question clicked on the previous run


def _ask(question: str, workflow_id: str | None) -> None:
    history = st.session_state[HISTORY_KEY]
    turns = [{"role": m["role"], "content": m["content"]} for m in history]
    history.append({"role": "user", "content": question})
    try:
        r = get_api().chat(question, turns, workflow_id)
        history.append({"role": "assistant", "content": r["answer"], "sources": r["sources"],
                        "note": r.get("note"), "provider": r["provider"]})
    except APIError as exc:
        history.append({"role": "assistant", "content": f"⚠️ {exc.detail}", "sources": [], "provider": "error"})


def _show(msg: dict) -> None:
    with st.chat_message(msg["role"], avatar="🧑‍💻" if msg["role"] == "user" else "🤖"):
        st.markdown(msg["content"])
        if msg.get("note"):
            st.caption(f"ℹ️ {msg['note']}")
        if msg.get("sources"):
            st.caption("📚 Sources: " + " · ".join(msg["sources"]))


def render() -> None:
    st.title("Project Assistant")
    st.write(
        "Ask anything about this project - how the pipeline, risk model, quality gate and self-healing loop work, "
        "the tests, CI/CD and deployment - or about one of your workflow runs. The assistant only **reads**: it "
        "never runs pipelines, approves fixes or changes code."
    )
    require_api()
    api = get_api()
    info = api.assistant_info()
    if info["provider"] == "anthropic":
        st.caption(f"🧠 Answers written by Claude (`{info['model']}`), grounded in the project docs and live data.")
    else:
        st.caption("📴 Offline mode: answers come from the project knowledge base and live data (no LLM). "
                   "Set `LLM_PROVIDER=anthropic` and `LLM_API_KEY` for Claude-written answers.")

    st.session_state.setdefault(HISTORY_KEY, [])
    wfs = api.workflows(limit=50)
    c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
    workflow_id = c1.selectbox(
        "Ask about a workflow (optional)", [None] + [w["id"] for w in wfs],
        format_func=lambda i: "— none: general questions —" if i is None else next(
            f"{w['id']} · {w['scenario']} · {w['state']}" for w in wfs if w["id"] == i),
        help="Questions like 'why did this run fail?' or 'what happens next?' are answered from this run's trace.",
    )
    if c2.button("🗑️ Clear chat", width="stretch", disabled=not st.session_state[HISTORY_KEY]):
        st.session_state[HISTORY_KEY] = []
        st.rerun()

    pending = st.session_state.pop(PENDING_KEY, None)
    if not st.session_state[HISTORY_KEY] and not pending:
        st.markdown("##### Try asking")
        cols = st.columns(3)
        for i, q in enumerate(info["suggestions"]):
            if cols[i % 3].button(q, key=f"suggest-{i}", width="stretch"):
                st.session_state[PENDING_KEY] = q
                st.rerun()

    for msg in st.session_state[HISTORY_KEY]:
        _show(msg)

    question = st.chat_input("Ask about the project or a workflow…") or pending
    if question:
        _show({"role": "user", "content": question})
        with st.spinner("Thinking…"):
            _ask(question, workflow_id)
        _show(st.session_state[HISTORY_KEY][-1])
