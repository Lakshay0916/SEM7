"""Headless smoke tests of every Streamlit page (no browser needed)."""

import pytest
from streamlit.testing.v1 import AppTest


def _page(module: str) -> AppTest:
    def script(module: str):
        import importlib
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path.cwd()))
        importlib.import_module(module).render()

    return AppTest.from_function(script, kwargs={"module": module}, default_timeout=120)


@pytest.fixture(autouse=True)
def isolated_backend(tmp_path, monkeypatch):
    """UI talks to the real FastAPI app in-process, backed by a throwaway database and workspace."""
    from dataclasses import replace

    import streamlit as st

    import app.api.main as api
    import app.ci.simulator as sim_mod
    import app.orchestration.store as store_mod

    patched = replace(store_mod.settings, database_url=f"sqlite:///{tmp_path / 'ui.sqlite3'}",
                      workspace_dir=tmp_path / "ws")
    monkeypatch.setattr(store_mod, "settings", patched)
    monkeypatch.setattr(sim_mod, "settings", patched)
    monkeypatch.setenv("SEM7_API_INPROCESS", "1")
    api.get_store.cache_clear()
    st.cache_resource.clear()
    yield
    api.get_store.cache_clear()
    st.cache_resource.clear()


@pytest.mark.parametrize(
    "module", ["ui.views.dashboard", "ui.views.how_it_works", "ui.views.workflows", "ui.views.risk_model"]
)
def test_page_renders_without_errors(module):
    at = _page(module).run()
    assert not at.exception, at.exception


def test_run_pipeline_button_produces_failed_workflow():
    at = _page("ui.views.run_pipeline").run()
    assert not at.exception
    at.radio[0].set_value("simple_bug").run()
    at.button[0].click().run()
    assert not at.exception, at.exception
    assert "current_wf" in at.session_state
    captions = " ".join(c.value for c in at.caption)
    assert "final state **WAITING_APPROVAL**" in captions  # a fix is proposed, nothing applied yet
    html = " ".join(m.value for m in at.markdown)
    for title in ("Change analysis", "ML risk prediction", "CI test run", "Failure evidence packaged",
                  "Self-healing loop", "Workflow recorded"):
        assert title in html


def test_approve_button_repairs_and_opens_pr():
    at = _page("ui.views.run_pipeline").run()
    at.radio[0].set_value("simple_bug").run()
    at.button[0].click().run()
    wf = at.session_state["current_wf"]
    at.button(key=f"approve-{wf}").click().run()
    assert not at.exception, at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "final state **PR_CREATED**" in captions
    assert any("not merged" in s.value for s in at.success)


def test_workflow_history_replays_a_run():
    at = _page("ui.views.run_pipeline").run()
    at.radio[0].set_value("high_risk_change").run()
    at.button[0].click().run()
    hist = _page("ui.views.workflows").run()
    assert not hist.exception, hist.exception
    assert "final state **PASSED**" in " ".join(c.value for c in hist.caption)


def test_ui_reports_unreachable_backend(monkeypatch):
    import streamlit as st

    monkeypatch.delenv("SEM7_API_INPROCESS")
    monkeypatch.setenv("SEM7_API_URL", "http://127.0.0.1:9")  # nothing listens here
    st.cache_resource.clear()
    at = _page("ui.views.dashboard").run()
    assert not at.exception
    assert any("Backend API unavailable" in e.value for e in at.error)
