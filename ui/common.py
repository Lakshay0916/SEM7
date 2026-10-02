"""Shared UI helpers: badges and charts. Data comes from the backend API (ui/api_client.py)."""

from __future__ import annotations

import altair as alt
import pandas as pd

from ui.style import is_dark

BAR_COLOR = {"light": "#2a78d6", "dark": "#3987e5"}  # single-series magnitude hue per theme
LABEL_COLOR = {"light": "#3d4757", "dark": "#c3cad6"}

RISK_BADGE = {"LOW": "🟢 LOW", "MEDIUM": "🟠 MEDIUM", "HIGH": "🔴 HIGH"}
TEST_BADGE = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "⚠️ ERROR", "TIMEOUT": "⏱️ TIMEOUT"}
GATE_BADGE = {"PASS": "✅ PASS", "REVIEW": "🟠 REVIEW", "BLOCK": "⛔ BLOCK"}


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
