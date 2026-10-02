"""Shared helpers for the Streamlit UI: cached resources, charts, safety demo."""

from __future__ import annotations

import json
import subprocess

import altair as alt
import pandas as pd
import streamlit as st

from app.config import PROJECT_ROOT
from app.git.repo import GitRepo, ProtectedBranchError
from app.orchestration.store import Store
from app.risk.predictor import MODEL_PATH, RiskPredictor
from ui.style import is_dark

RISK_DIR = PROJECT_ROOT / "data" / "risk"
BAR_COLOR = {"light": "#2a78d6", "dark": "#3987e5"}  # single-series magnitude hue per theme
LABEL_COLOR = {"light": "#3d4757", "dark": "#c3cad6"}

RISK_BADGE = {"LOW": "🟢 LOW", "MEDIUM": "🟠 MEDIUM", "HIGH": "🔴 HIGH"}
TEST_BADGE = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "⚠️ ERROR", "TIMEOUT": "⏱️ TIMEOUT"}
IMPLEMENTED_PHASE = 4


@st.cache_resource
def get_store() -> Store:
    return Store()


@st.cache_resource
def get_predictor() -> RiskPredictor | None:
    return RiskPredictor.load() if MODEL_PATH.exists() else None


@st.cache_data
def load_json(name: str):
    path = RISK_DIR / name
    return json.loads(path.read_text()) if path.exists() else None


def hbar(df: pd.DataFrame, cat: str, val: str, fmt: str, x_title: str, sort="-x") -> alt.Chart:
    """Single-series horizontal bar chart: thin bars, rounded data end, value at tip, tooltip."""
    base = alt.Chart(df).encode(
        y=alt.Y(f"{cat}:N", sort=sort, title=None, axis=alt.Axis(labelLimit=260)),
        x=alt.X(f"{val}:Q", title=x_title, axis=alt.Axis(format=fmt, gridOpacity=0.35, tickCount=5)),
        tooltip=[alt.Tooltip(f"{cat}:N"), alt.Tooltip(f"{val}:Q", format=fmt)],
    )
    mode = "dark" if is_dark() else "light"
    bars = base.mark_bar(color=BAR_COLOR[mode], cornerRadiusEnd=4, size=18)
    labels = base.mark_text(align="left", dx=4, fontSize=12, color=LABEL_COLOR[mode]).encode(
        text=alt.Text(f"{val}:Q", format=fmt)
    )
    return (bars + labels).properties(height=alt.Step(30))


def try_ai_commit_to_main(repo_path: str) -> str:
    ai = GitRepo(repo_path, actor="ai")
    original = ai.current_branch()
    probe = ai.path / "AI_WAS_HERE.txt"
    main_before = ai.head_sha("main")
    try:
        ai.checkout("main")
        probe.write_text("unauthorised change\n")
        ai.commit_all("AI tries to modify main")
        result = "❗ UNEXPECTED: commit succeeded"
    except ProtectedBranchError as exc:
        result = f"✅ Blocked - ProtectedBranchError: {exc}"
    finally:
        probe.unlink(missing_ok=True)
        subprocess.run(["git", "checkout", "-q", original], cwd=ai.path, check=False)
    unchanged = ai.head_sha("main") == main_before
    return f"{result}\nmain unchanged: {unchanged} (still at {main_before[:10]})"
