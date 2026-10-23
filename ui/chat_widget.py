"""Floating project assistant: a chat button fixed bottom-right on every page that opens a chat panel.

The panel content only runs while it is open (popover on_change="rerun"), so closed it costs no API calls.
"""

from __future__ import annotations

import streamlit as st

from ui.api_client import APIError, get_api

HISTORY_KEY = "assistant_history"  # [{role, content, sources?, note?, provider?}]
FAB_KEY = "chat_fab"               # CSS hook: .st-key-chat_fab (see ui/style.py)


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
            st.caption("📚 " + " · ".join(msg["sources"]))


def chat_panel() -> None:
    """The chat itself (rendered inside the popover; also testable on its own)."""
    st.session_state.setdefault(HISTORY_KEY, [])
    try:
        api = get_api()
        info = api.assistant_info()
        wfs = api.workflows(limit=30)
    except APIError as exc:
        st.warning(f"Assistant unavailable - {exc.detail}")
        return

    with st.container(key="chat_panel"):
        head, clear = st.columns([5, 2], vertical_alignment="center")
        head.markdown("**💬 SEM7 Assistant**")
        if clear.button("Clear", key="chat_clear", icon=":material/delete:", type="tertiary"):
            st.session_state[HISTORY_KEY] = []
        st.caption(f"🧠 Claude (`{info['model']}`)" if info["provider"] == "anthropic"
                   else "📴 Offline mode · answers from project docs + live data")

        ids = [None] + [w["id"] for w in wfs]
        current = st.session_state.get("current_wf")  # the run open on Run Pipeline, if any
        workflow_id = st.selectbox(
            "About a workflow (optional)", ids, index=ids.index(current) if current in ids else 0,
            key="chat_wf",
            format_func=lambda i: "General questions" if i is None else next(
                f"{w['id']} · {w['scenario'] or '-'} · {w['state']}" for w in wfs if w["id"] == i),
        )

        messages = st.container(height=360, border=False)
        suggestions = messages.empty()
        picked = None
        if not st.session_state[HISTORY_KEY]:
            with suggestions.container():
                st.caption("Read-only: I explain the project and your runs; I never change anything. Try:")
                for i, q in enumerate(info["suggestions"][:4]):
                    if st.button(q, key=f"suggest-{i}", width="stretch"):
                        picked = q
        with messages:
            for msg in st.session_state[HISTORY_KEY]:
                _show(msg)

        question = st.chat_input("Ask about the project or a run…", key="chat_input") or picked
        if question:
            suggestions.empty()
            with messages:
                _show({"role": "user", "content": question})
                with st.spinner("Thinking…"):
                    _ask(question, workflow_id)
                _show(st.session_state[HISTORY_KEY][-1])


def render_chat_widget() -> None:
    pop = st.popover("Ask the assistant", icon=":material/chat:", key=FAB_KEY, on_change="rerun")
    if pop.open:
        with pop:
            chat_panel()
