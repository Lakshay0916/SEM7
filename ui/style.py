"""Visual building blocks: global CSS and small HTML components.

The CSS is theme-agnostic: surfaces are translucent tints over whatever the
active Streamlit theme paints, and text inherits the theme's text color, so the
same styles read correctly in light and dark mode. Status is always icon + label,
never color alone.
"""

from __future__ import annotations

import html

import streamlit as st

CSS = """
<style>
.block-container { padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1220px; }

/* Gradient page titles */
h1 { background: linear-gradient(90deg, #2a78d6 0%, #7c5cf0 100%);
     -webkit-background-clip: text; background-clip: text; color: transparent !important;
     letter-spacing: -0.03em; padding-bottom: .1rem; }

/* KPI tiles: every st.metric becomes a card */
div[data-testid="stMetric"] {
  background: rgba(42,120,214,0.06); border: 1px solid rgba(128,145,170,0.22);
  border-radius: 14px; padding: .85rem 1rem .8rem 1rem; height: 100%;
}
div[data-testid="stMetricLabel"] p { font-size: .8rem; font-weight: 600; opacity: .75;
  text-transform: uppercase; letter-spacing: .04em; }
div[data-testid="stMetricValue"] { font-size: 1.5rem; font-weight: 700; }

/* Hero banner */
.hero { position: relative; overflow: hidden; padding: 1.5rem 1.7rem; border-radius: 18px; margin-bottom: 1.3rem;
  background: linear-gradient(135deg, rgba(42,120,214,0.16) 0%, rgba(124,92,240,0.12) 55%, rgba(27,175,122,0.08) 100%);
  border: 1px solid rgba(42,120,214,0.28); }
.hero h2 { margin: 0 0 .45rem 0; font-size: 1.35rem; font-weight: 750; letter-spacing: -0.01em; }
.hero p { margin: 0; opacity: .85; font-size: .99rem; line-height: 1.6; max-width: 62rem; }
.chips { margin-top: .9rem; display: flex; gap: .45rem; flex-wrap: wrap; }
.chip { font-size: .78rem; font-weight: 650; padding: .28rem .7rem; border-radius: 999px;
  background: rgba(42,120,214,0.14); border: 1px solid rgba(42,120,214,0.30); }

/* Step cards: keyed containers get a status stripe */
div[class*="st-key-step-"] { border-left-width: 5px !important; }
div[class*="st-key-step-done"] { border-left-color: #22a35a !important; }
div[class*="st-key-step-fail"] { border-left-color: #d64545 !important; }
div[class*="st-key-step-warn"] { border-left-color: #e19a17 !important; }
div[class*="st-key-step-plan"] { border-left-color: #8b95a7 !important; }

.step-head { display: flex; align-items: center; gap: .75rem; margin-bottom: .45rem; flex-wrap: wrap; }
.step-num { width: 32px; height: 32px; flex: 0 0 32px; border-radius: 10px; color: #fff; font-weight: 800; font-size: .92rem;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #2a78d6, #7c5cf0); box-shadow: 0 2px 8px rgba(42,120,214,0.35); }
.step-title { font-size: 1.14rem; font-weight: 700; letter-spacing: -0.01em; }

/* Solid pills read on both themes */
.pill { padding: 3px 11px; border-radius: 999px; font-size: .76rem; font-weight: 700; white-space: nowrap; color: #fff; }
.pill-done { background: #198a4b; }
.pill-fail { background: #c53030; }
.pill-warn { background: #b86e00; }
.pill-plan { background: #64708a; }

.explain { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin: .5rem 0 .9rem 0; }
.explain div { background: rgba(128,145,170,0.09); border: 1px solid rgba(128,145,170,0.16);
  border-radius: 12px; padding: .7rem .85rem; font-size: .9rem; line-height: 1.55; }
.explain div > span { opacity: .88; }
.explain b { display: block; font-size: .7rem; text-transform: uppercase; letter-spacing: .07em; opacity: .65; margin-bottom: .25rem; }
.explain .why b::before { content: "💡 "; }
.explain .what b::before { content: "⚙️ "; }
@media (max-width: 820px) { .explain { grid-template-columns: 1fr; } }

/* Progress strip */
.progress { display: flex; gap: 4px; flex-wrap: wrap; margin: .5rem 0 .4rem 0; }
.progress .p { flex: 1 1 0; min-width: 58px; text-align: center; padding: .5rem .2rem; border-radius: 10px;
  font-size: .76rem; font-weight: 650; border: 1px solid rgba(128,145,170,0.28); }
.progress .p.done { background: rgba(34,163,90,0.16); border-color: rgba(34,163,90,0.55); }
.progress .p.fail { background: rgba(214,69,69,0.16); border-color: rgba(214,69,69,0.6); }
.progress .p.plan { border-style: dashed; opacity: .55; }

/* Risk gauge */
.gauge { margin: .3rem 0 .35rem 0; }
.gauge-track { position: relative; height: 14px; border-radius: 7px; display: flex; overflow: hidden; }
.gauge-track span { height: 100%; }
.gauge-marker { position: relative; height: 24px; }
.gauge-marker div { position: absolute; top: 2px; transform: translateX(-50%); font-size: .82rem; font-weight: 800; white-space: nowrap; }
.gauge-labels { display: flex; justify-content: space-between; font-size: .74rem; opacity: .7; margin-top: .25rem; }

/* Upcoming stages */
.upcoming { display: grid; grid-template-columns: repeat(auto-fill, minmax(255px, 1fr)); gap: .75rem; }
.upcoming div { border: 1px dashed rgba(128,145,170,0.45); border-radius: 12px; padding: .75rem .9rem; background: rgba(128,145,170,0.05); }
.upcoming b { display: block; font-size: .93rem; }
.upcoming small { opacity: .75; font-size: .82rem; line-height: 1.45; display: block; margin-top: .25rem; }
.upcoming .ph { float: right; font-size: .7rem; font-weight: 700; opacity: .6; }

/* Key/value tiles (wrap long values instead of truncating) */
.kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .7rem; margin: .2rem 0 .6rem 0; }
.kv div { background: rgba(42,120,214,0.06); border: 1px solid rgba(128,145,170,0.22); border-radius: 14px; padding: .7rem .9rem; }
.kv small { display: block; font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; opacity: .65; }
.kv span { display: block; font-size: 1.02rem; font-weight: 700; margin-top: .2rem; word-break: break-word; }

/* Feature tiles (dashboard) */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: .8rem; margin: .4rem 0 1.2rem 0; }
.tile { border-radius: 14px; padding: .95rem 1rem; border: 1px solid rgba(128,145,170,0.22); background: rgba(128,145,170,0.06); }
.tile .ic { font-size: 1.35rem; }
.tile b { display: block; margin: .3rem 0 .2rem 0; font-size: .97rem; }
.tile small { opacity: .75; line-height: 1.45; font-size: .84rem; }

/* Sidebar brand */
.brand { padding: .2rem 0 .6rem 0; }
.brand .logo { font-size: 1.05rem; font-weight: 800; letter-spacing: -0.01em;
  background: linear-gradient(90deg, #2a78d6, #7c5cf0); -webkit-background-clip: text; background-clip: text; color: transparent; }
.brand small { display: block; opacity: .7; font-size: .78rem; line-height: 1.45; margin-top: .25rem; }

code { font-size: .86em; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def is_dark() -> bool:
    """Active theme type (used for chart/diagram colors that CSS cannot reach)."""
    try:
        return st.context.theme.type == "dark"
    except Exception:
        return False


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
        <div class="explain"><div class="what"><b>What happens</b><span>{esc(what)}</span></div>
        <div class="why"><b>Why it matters</b><span>{esc(why)}</span></div></div>""",
        unsafe_allow_html=True,
    )


def progress_bar(items: list[tuple[str, str]]) -> None:
    """items: (label, state) with state in done | fail | plan."""
    icon = {"done": "✓ ", "fail": "✗ ", "plan": ""}
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
          <span style="width:{lo:.1f}%;background:#3fb46b"></span>
          <span style="width:{mid:.1f}%;background:#f0b429"></span>
          <span style="width:{hi:.1f}%;background:#e05252"></span>
        </div>
        <div class="gauge-labels"><span>0 · LOW</span><span>MEDIUM ≥ {medium:.2f}</span>
        <span>HIGH ≥ {high:.2f} · 1</span></div></div>""",
        unsafe_allow_html=True,
    )


def upcoming_cards(items: list[tuple[str, int, str]]) -> None:
    cards = "".join(
        f'<div><span class="ph">PHASE {ph}</span><b>⏳ {esc(t)}</b><small>{esc(d)}</small></div>'
        for t, ph, d in items
    )
    st.markdown(f'<div class="upcoming">{cards}</div>', unsafe_allow_html=True)


def hero(title: str, body: str, chips: list[str] | None = None) -> None:
    chip_html = (
        '<div class="chips">' + "".join(f'<span class="chip">{esc(c)}</span>' for c in chips) + "</div>"
        if chips else ""
    )
    st.markdown(f'<div class="hero"><h2>{esc(title)}</h2><p>{esc(body)}</p>{chip_html}</div>',
                unsafe_allow_html=True)


def tiles(items: list[tuple[str, str, str]]) -> None:
    """(icon, title, description) feature tiles."""
    html_ = "".join(f'<div class="tile"><span class="ic">{i}</span><b>{esc(t)}</b><small>{esc(d)}</small></div>'
                    for i, t, d in items)
    st.markdown(f'<div class="tiles">{html_}</div>', unsafe_allow_html=True)


def kv_tiles(items: dict[str, str]) -> None:
    cells = "".join(f"<div><small>{esc(k)}</small><span>{esc(v)}</span></div>" for k, v in items.items())
    st.markdown(f'<div class="kv">{cells}</div>', unsafe_allow_html=True)


def sidebar_brand() -> None:
    st.sidebar.markdown(
        '<div class="brand"><div class="logo">🛠️ SEM7 · Self-Healing CI/CD</div>'
        "<small>ML predicts · RAG retrieves · LLM reasons · agents act · tests verify</small></div>",
        unsafe_allow_html=True,
    )
