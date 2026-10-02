"""Visual building blocks: global CSS and small HTML components (status always icon + label)."""

from __future__ import annotations

import html

import streamlit as st

CSS = """
<style>
.block-container { padding-top: 2rem; max-width: 1200px; }
h1 { font-weight: 700; letter-spacing: -0.02em; }
div[data-testid="stMetricValue"] { font-size: 1.45rem; }

.hero { padding: 1.4rem 1.6rem; border-radius: 14px; background: #f4f7fc; border: 1px solid #e1e7f0; margin-bottom: 1.2rem; }
.hero h2 { margin: 0 0 .35rem 0; font-size: 1.35rem; }
.hero p { margin: 0; color: #4a5568; font-size: .98rem; line-height: 1.55; }
.motto { display: inline-block; margin-top: .7rem; font-size: .85rem; color: #2a78d6; font-weight: 600; }

.step-head { display: flex; align-items: center; gap: .75rem; margin-bottom: .35rem; flex-wrap: wrap; }
.step-num { width: 30px; height: 30px; flex: 0 0 30px; border-radius: 50%; background: #2a78d6; color: #fff;
            display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: .9rem; }
.step-num.muted { background: #c5ccd8; }
.step-title { font-size: 1.12rem; font-weight: 650; color: #1d2433; }
.pill { padding: 2px 10px; border-radius: 999px; font-size: .78rem; font-weight: 650; white-space: nowrap; }
.pill-done { background: #e6f5e6; color: #106b10; }
.pill-fail { background: #fdeaea; color: #a12727; }
.pill-warn { background: #fff4dc; color: #8a5a00; }
.pill-plan { background: #eef0f4; color: #5b6475; }
.explain { display: grid; grid-template-columns: 1fr 1fr; gap: .9rem; margin: .4rem 0 .8rem 0; }
.explain div { background: #f7f9fc; border-radius: 10px; padding: .65rem .8rem; font-size: .9rem; color: #3d4757; line-height: 1.5; }
.explain b { display: block; font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; color: #6b7588; margin-bottom: .2rem; }
@media (max-width: 800px) { .explain { grid-template-columns: 1fr; } }

.progress { display: flex; gap: 4px; flex-wrap: wrap; margin: .3rem 0 1rem 0; }
.progress .p { flex: 1 1 0; min-width: 74px; text-align: center; padding: .45rem .3rem; border-radius: 8px;
               font-size: .76rem; font-weight: 600; border: 1px solid #e1e7f0; background: #fff; color: #6b7588; }
.progress .p.done { background: #e6f5e6; border-color: #bfe3bf; color: #106b10; }
.progress .p.fail { background: #fdeaea; border-color: #f2c4c4; color: #a12727; }
.progress .p.stop { background: #f4f7fc; border-color: #c9d6ea; color: #2a5ea8; }
.progress .p.plan { background: #fafbfc; border-style: dashed; color: #9aa3b2; }

.gauge { margin: .4rem 0 .2rem 0; }
.gauge-track { position: relative; height: 14px; border-radius: 7px; display: flex; overflow: hidden; }
.gauge-track span { height: 100%; }
.gauge-marker { position: relative; height: 22px; }
.gauge-marker div { position: absolute; top: 2px; transform: translateX(-50%); font-size: .8rem; font-weight: 700; color: #1d2433; white-space: nowrap; }
.gauge-labels { display: flex; justify-content: space-between; font-size: .74rem; color: #6b7588; }

.upcoming { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .7rem; }
.upcoming div { border: 1px dashed #cdd5e1; border-radius: 10px; padding: .7rem .85rem; background: #fafbfc; }
.upcoming b { display: block; color: #3d4757; font-size: .92rem; }
.upcoming small { color: #6b7588; font-size: .82rem; line-height: 1.45; display: block; margin-top: .2rem; }
.upcoming .ph { float: right; font-size: .72rem; color: #8a93a3; font-weight: 600; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(text: str) -> str:
    """Escape, then render `code` spans."""
    out = html.escape(text)
    parts = out.split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


PILL_CLASS = {"done": "pill-done", "fail": "pill-fail", "warn": "pill-warn", "plan": "pill-plan"}


def step_header(num: int | str, title: str, status: str, label: str, what: str, why: str) -> None:
    st.markdown(
        f"""<div class="step-head"><div class="step-num">{num}</div>
        <div class="step-title">{esc(title)}</div>
        <span class="pill {PILL_CLASS[status]}">{esc(label)}</span></div>
        <div class="explain"><div><b>What happens</b>{esc(what)}</div><div><b>Why it matters</b>{esc(why)}</div></div>""",
        unsafe_allow_html=True,
    )


def progress_bar(items: list[tuple[str, str]]) -> None:
    """items: (label, state) with state in done | fail | stop | plan."""
    icon = {"done": "✓ ", "fail": "✗ ", "stop": "■ ", "plan": ""}
    cells = "".join(f'<div class="p {s}">{icon[s]}{esc(lbl)}</div>' for lbl, s in items)
    st.markdown(f'<div class="progress">{cells}</div>', unsafe_allow_html=True)


def risk_gauge(score: float, medium: float, high: float) -> None:
    """Score position over LOW / MEDIUM / HIGH bands (bands from validation-score percentiles)."""
    lo, mid = medium * 100, (high - medium) * 100
    hi = 100 - lo - mid
    pos = min(max(score, 0.0), 1.0) * 100
    st.markdown(
        f"""<div class="gauge">
        <div class="gauge-marker"><div style="left:{pos:.1f}%">▼ {score:.3f}</div></div>
        <div class="gauge-track">
          <span style="width:{lo:.1f}%;background:#9fd59f"></span>
          <span style="width:{mid:.1f}%;background:#f6cf74"></span>
          <span style="width:{hi:.1f}%;background:#ec9a9a"></span>
        </div>
        <div class="gauge-labels"><span>0 · LOW</span><span>MEDIUM from {medium:.2f}</span>
        <span>HIGH from {high:.2f} · 1</span></div></div>""",
        unsafe_allow_html=True,
    )


def upcoming_cards(items: list[tuple[str, int, str]]) -> None:
    cards = "".join(
        f'<div><span class="ph">Phase {ph}</span><b>⏳ {esc(t)}</b><small>{esc(d)}</small></div>'
        for t, ph, d in items
    )
    st.markdown(f'<div class="upcoming">{cards}</div>', unsafe_allow_html=True)


def hero(title: str, body: str, motto: str | None = None) -> None:
    m = f'<span class="motto">{esc(motto)}</span>' if motto else ""
    st.markdown(f'<div class="hero"><h2>{esc(title)}</h2><p>{esc(body)}</p>{m}</div>', unsafe_allow_html=True)
