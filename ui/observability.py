"""Charts for tracing and monitoring: the trace waterfall and the per-agent heat maps."""

from __future__ import annotations

import altair as alt
import pandas as pd

from ui.style import is_dark

# Span kinds folded into six colour classes (categorical slots 1-5 in fixed order, structure in gray).
KIND_CLASS = {"agent": "agent", "delegation": "agent", "llm": "LLM call", "tool": "tool call (MCP)",
              "guardrail": "guardrail", "a2a": "A2A hop"}
CLASSES = ["agent", "LLM call", "tool call (MCP)", "guardrail", "A2A hop", "structure"]
COLORS = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#a3a29c"],
          "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#6f6e69"]}
# One-hue sequential ramp; "near zero" recedes into the surface in each theme.
RAMP = {"light": ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#104281"],
        "dark": ["#184f95", "#256abf", "#3987e5", "#86b6ef", "#cde2fb"]}
INK = {"light": ("#0b0b0b", "#ffffff"), "dark": ("#ffffff", "#0b0b0b")}  # text on (low, high) cells


def _mode() -> str:
    return "dark" if is_dark() else "light"


def span_table(spans: list[dict]) -> pd.DataFrame:
    """Spans in tree order (each parent before its children) with depth and time offsets."""
    if not spans:
        return pd.DataFrame()
    children: dict = {}
    ids = {s["span_id"] for s in spans}
    for s in sorted(spans, key=lambda s: s["start"]):
        children.setdefault(s["parent_id"] if s["parent_id"] in ids else None, []).append(s)
    t0 = min(s["start"] for s in spans)
    rows: list[dict] = []

    def walk(parent, depth):
        for s in children.get(parent, []):
            a = s.get("attributes") or {}
            tokens = (a.get("prompt_tokens") or 0) + (a.get("output_tokens") or 0)
            rows.append({"row": len(rows), "label": " " * depth + s["name"], "name": s["name"],
                         "kind": s["kind"], "class": KIND_CLASS.get(s["kind"], "structure"),
                         "framework": s.get("framework") or "", "start_s": round(s["start"] - t0, 3),
                         "end_s": round(s["end"] - t0, 3), "seconds": s["seconds"], "status": s["status"],
                         "tokens": tokens or None, "model": a.get("model") or "",
                         "detail": a.get("verdict") or a.get("args") or a.get("task") or a.get("error") or ""})
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    df = pd.DataFrame(rows)
    # instant spans (a guardrail check, a cached tool call) still get a visible sliver
    df["end_vis"] = df[["end_s", "start_s"]].max(axis=1).clip(lower=df["start_s"] + max(df["end_s"].max(), 1) / 250)
    return df


def waterfall(df: pd.DataFrame) -> alt.Chart:
    """Trace waterfall: one row per span in tree order, a bar from its start to its end."""
    mode = _mode()
    labels = df["label"].tolist()
    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort=labels, title=None, axis=alt.Axis(labelLimit=340, labelFontSize=11)),
        tooltip=[alt.Tooltip("name:N", title="span"), alt.Tooltip("kind:N"), alt.Tooltip("framework:N"),
                 alt.Tooltip("seconds:Q", format=".2f", title="seconds"), alt.Tooltip("start_s:Q", format=".2f",
                                                                                     title="starts at (s)"),
                 alt.Tooltip("tokens:Q", format=","), alt.Tooltip("model:N"), alt.Tooltip("status:N"),
                 alt.Tooltip("detail:N")],
    )
    bars = base.mark_bar(cornerRadius=3, height=12).encode(
        x=alt.X("start_s:Q", title="seconds since the attempt started", axis=alt.Axis(gridOpacity=0.3)),
        x2="end_vis:Q",
        color=alt.Color("class:N", scale=alt.Scale(domain=CLASSES, range=COLORS[mode]),
                        legend=alt.Legend(title=None, orient="top", direction="horizontal")),
    )
    errors = base.transform_filter(alt.datum.status == "error").mark_point(
        shape="cross", size=70, strokeWidth=2, color="#e34948" if mode == "light" else "#e66767").encode(
        x="end_s:Q")
    return (bars + errors).properties(height=alt.Step(17))


def metric_heatmap(agents: pd.DataFrame, metrics: dict[str, str]) -> alt.Chart:
    """Agents x metrics. Each column is coloured relative to its own maximum (different units);
    the cell shows the real value."""
    mode = _mode()
    long = agents.melt(id_vars=["Agent"], value_vars=list(metrics), var_name="metric", value_name="value")
    long["metric"] = long["metric"].map(metrics)
    long["share"] = long.groupby("metric")["value"].transform(lambda v: v / v.max() if v.max() else 0)
    long["text"] = long["value"].map(lambda v: f"{v:,.0f}" if v >= 100 else f"{v:,.2f}".rstrip("0").rstrip("."))
    base = alt.Chart(long).encode(
        x=alt.X("metric:N", sort=list(metrics.values()), title=None,
                axis=alt.Axis(orient="top", labelAngle=0, labelLimit=140)),
        y=alt.Y("Agent:N", title=None, axis=alt.Axis(labelLimit=220)),
        tooltip=[alt.Tooltip("Agent:N"), alt.Tooltip("metric:N"), alt.Tooltip("value:Q", format=",.2f")],
    )
    cells = base.mark_rect(cornerRadius=3, stroke="transparent", strokeWidth=2).encode(
        color=alt.Color("share:Q", scale=alt.Scale(domain=[0, 1], range=RAMP[mode]),
                        legend=alt.Legend(title="share of column max", format=".0%", orient="bottom",
                                          gradientLength=180)))
    low, high = INK[mode]  # dark text on light cells, light text on dark cells
    text_low = base.transform_filter(alt.datum.share <= 0.55).mark_text(fontSize=12, color=low).encode(text="text:N")
    text_high = base.transform_filter(alt.datum.share > 0.55).mark_text(fontSize=12, color=high).encode(text="text:N")
    return (cells + text_low + text_high).properties(height=alt.Step(34))


def run_heatmap(cells: pd.DataFrame, value: str, title: str, fmt: str, order: list[str]) -> alt.Chart:
    """Agents x runs: how long (or how many tokens) each agent took in each traced attempt.

    `cells["run"]` is the column label; `order` lists the labels oldest first."""
    mode = _mode()
    base = alt.Chart(cells).encode(
        x=alt.X("run:N", sort=order, title="traced attempt (oldest → newest)",
                axis=alt.Axis(labelAngle=-45, labelLimit=200)),
        y=alt.Y("agent:N", title=None, axis=alt.Axis(labelLimit=220)),
        tooltip=[alt.Tooltip("agent:N"), alt.Tooltip("run:N", title="attempt"), alt.Tooltip("pattern:N"),
                 alt.Tooltip(f"{value}:Q", format=fmt, title=title), alt.Tooltip("llm_calls:Q", title="LLM calls"),
                 alt.Tooltip("tool_calls:Q", title="tool calls")],
    )
    return base.mark_rect(cornerRadius=2, stroke="transparent", strokeWidth=2).encode(
        color=alt.Color(f"{value}:Q", scale=alt.Scale(range=RAMP[mode], zero=True),
                        legend=alt.Legend(title=title, orient="bottom", gradientLength=180))
    ).properties(height=alt.Step(28))
