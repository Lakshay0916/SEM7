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


PATTERN_COLORS = {"light": ["#2a78d6", "#eb6834", "#1baf7a"], "dark": ["#3987e5", "#d95926", "#199e70"]}
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}


def boxplot(df: pd.DataFrame, cat: str, val: str, hue: str, order: list[str], hue_order: list[str], title: str,
            fmt: str, tooltip: list[str], log: bool = False) -> alt.Chart:
    """Box (min-max whiskers, quartiles, median) per category, with every run drawn as a dot on top -
    small samples, so the individual runs stay visible. `log` for heavily skewed measures (time)."""
    mode = "dark" if is_dark() else "light"
    ink = "#0b0b0b" if mode == "light" else "#f0efec"
    colors = alt.Color(f"{hue}:N", scale=alt.Scale(domain=hue_order, range=PATTERN_COLORS[mode][:len(hue_order)]),
                       legend=alt.Legend(title=None, orient="top", direction="horizontal"))
    x = alt.X(f"{cat}:N", sort=order, title=None, axis=alt.Axis(labelAngle=-20, labelLimit=200))
    y = alt.Y(f"{val}:Q", title=title + (" - log scale" if log else ""), axis=alt.Axis(format=fmt, gridOpacity=0.35),
              scale=alt.Scale(type="log") if log else alt.Undefined)
    box = alt.Chart(df).mark_boxplot(extent="min-max", size=34, opacity=0.35,
                                     median={"strokeWidth": 2.5, "color": ink},
                                     rule={"strokeWidth": 1.5}, ticks={"size": 12}).encode(x=x, y=y, color=colors)
    dots = alt.Chart(df).mark_circle(size=55, opacity=0.95, stroke=SURFACE[mode], strokeWidth=1.5).encode(
        x=x, y=y, color=colors,
        tooltip=[alt.Tooltip(t) for t in tooltip] + [alt.Tooltip(f"{val}:Q", format=fmt, title=title)])
    return (box + dots).properties(height=320)
