"""Builds the SEM7 project guide (HTML with inline SVG diagrams) and prints it to PDF with Chrome.

Usage (needs `pip install playwright` and Google Chrome):
    python docs/build_guide.py docs/SEM7_Project_Guide.pdf
"""

import html
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT_HTML = Path(tempfile.gettempdir()) / "sem7_guide.html"
OUT_PDF = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("SEM7_Project_Guide.pdf"))

# ----------------------------------------------------------------------------- palette
INK, QUIET, EDGE, GRID = "#1a2233", "#5b6475", "#8a93a3", "#e2e8f0"
BLUE, GREEN, RED, AMBER, VIOLET = "#2a78d6", "#1f9d55", "#d64545", "#d98a00", "#6d5bd0"
FILL = {
    "normal": ("#ffffff", EDGE, 1.2, None),
    "accent": ("#eaf2fc", BLUE, 1.8, None),
    "good": ("#e8f6ee", GREEN, 1.6, None),
    "bad": ("#fdecec", RED, 1.6, None),
    "human": ("#fff4e0", AMBER, 1.8, None),
    "planned": ("#fafbfc", "#a9b1bf", 1.2, "5 4"),
    "violet": ("#f0eefc", VIOLET, 1.6, None),
}


def esc(s):
    return html.escape(s, quote=False)


def text_lines(cx, cy, lines, size=12.5, lh=15.5):
    """lines: list of str; first is bold name, rest quieter."""
    n = len(lines)
    y0 = cy - (n - 1) * lh / 2 + size * 0.35
    out = []
    for i, ln in enumerate(lines):
        bold = i == 0
        out.append(
            f"<text x='{cx}' y='{y0 + i * lh:.1f}' text-anchor='middle' font-size='{size if bold else size - 1.5}' "
            f"font-weight='{650 if bold else 400}' fill='{INK if bold else QUIET}'>{esc(ln)}</text>"
        )
    return "".join(out)


def box(cx, cy, w, h, lines, kind="normal", size=12.5):
    fill, stroke, sw, dash = FILL[kind]
    d = f" stroke-dasharray='{dash}'" if dash else ""
    return (f"<rect x='{cx - w / 2}' y='{cy - h / 2}' width='{w}' height='{h}' rx='9' fill='{fill}' "
            f"stroke='{stroke}' stroke-width='{sw}'{d}/>" + text_lines(cx, cy, lines, size))


def diamond(cx, cy, hw, hh, lines, kind="normal"):
    fill, stroke, sw, dash = FILL[kind]
    pts = f"{cx - hw},{cy} {cx},{cy - hh} {cx + hw},{cy} {cx},{cy + hh}"
    return f"<polygon points='{pts}' fill='{fill}' stroke='{stroke}' stroke-width='{sw}'/>" + text_lines(cx, cy, lines, 12)


def hexagon(cx, cy, w, h, lines, kind="human"):
    fill, stroke, sw, dash = FILL[kind]
    a = 16
    pts = (f"{cx - w / 2},{cy} {cx - w / 2 + a},{cy - h / 2} {cx + w / 2 - a},{cy - h / 2} "
           f"{cx + w / 2},{cy} {cx + w / 2 - a},{cy + h / 2} {cx - w / 2 + a},{cy + h / 2}")
    return f"<polygon points='{pts}' fill='{fill}' stroke='{stroke}' stroke-width='{sw}'/>" + text_lines(cx, cy, lines)


def cylinder(cx, cy, w, h, lines, kind="violet"):
    fill, stroke, sw, _ = FILL[kind]
    x0, y0, ry = cx - w / 2, cy - h / 2, 9
    return (f"<path d='M{x0} {y0 + ry} A{w / 2} {ry} 0 0 1 {x0 + w} {y0 + ry} V{y0 + h - ry} "
            f"A{w / 2} {ry} 0 0 1 {x0} {y0 + h - ry} Z' fill='{fill}' stroke='{stroke}' stroke-width='{sw}'/>"
            f"<path d='M{x0} {y0 + ry} A{w / 2} {ry} 0 0 0 {x0 + w} {y0 + ry}' fill='none' stroke='{stroke}' "
            f"stroke-width='{sw}'/>" + text_lines(cx, cy + 6, lines))


def arrow(d, color=EDGE, dashed=False, marker="a"):
    da = " stroke-dasharray='5 4'" if dashed else ""
    return (f"<path d='{d}' fill='none' stroke='{color}' stroke-width='1.4'{da} "
            f"marker-end='url(#{marker})'/>")


def label(x, y, s, anchor="middle", color=QUIET, size=11, weight=400):
    return (f"<text x='{x}' y='{y}' text-anchor='{anchor}' font-size='{size}' font-weight='{weight}' "
            f"fill='{color}'>{esc(s)}</text>")


def svg(h, body, title):
    defs = ("<defs>"
            f"<marker id='a' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='{EDGE}'/></marker>"
            f"<marker id='ag' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='{GREEN}'/></marker>"
            f"<marker id='ar' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='{RED}'/></marker>"
            f"<marker id='ab' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='{BLUE}'/></marker>"
            "</defs>")
    return (f"<svg viewBox='0 0 760 {h}' role='img' aria-label='{esc(title)}' "
            f"font-family='Inter, -apple-system, Helvetica, Arial, sans-serif'>{defs}"
            f"<text x='16' y='26' font-size='15' font-weight='700' fill='{INK}'>{esc(title)}</text>{body}</svg>")


# ----------------------------------------------------------------------------- diagrams

def d_big_picture():
    b = []
    cy = 90
    xs = [72, 198, 324, 450]
    b.append(box(xs[0], cy, 112, 56, ["Developer", "commits code"]))
    b.append(box(xs[1], cy, 112, 56, ["Diff analysis", "what changed?"]))
    b.append(box(xs[2], cy, 112, 56, ["ML risk score", "LOW / MED / HIGH"], "accent"))
    b.append(box(xs[3], cy, 112, 56, ["Real tests", "pytest runs"]))
    for a, c in zip(xs, xs[1:]):
        b.append(arrow(f"M{a + 56} {cy}H{c - 58}"))
    b.append(diamond(578, cy, 54, 40, ["Tests", "pass?"]))
    b.append(arrow(f"M{xs[3] + 56} {cy}H{578 - 56}"))
    b.append(box(706, cy, 92, 56, ["Finished", "gate verdict"], "good", 12))
    b.append(arrow(f"M{578 + 54} {cy}H{706 - 48}", GREEN, marker="ag"))
    b.append(label(645, cy - 9, "yes", color=GREEN, weight=600, size=10.5))
    # row B (right to left)
    rb = 220
    b.append(arrow(f"M578 {cy + 40}V{rb - 30}", RED, marker="ar"))
    b.append(label(590, cy + 66, "no", "start", RED, weight=600))
    b.append(box(578, rb, 128, 58, ["Investigate", "what failed? (facts)"]))
    b.append(box(424, rb, 128, 58, ["Root cause", "why? + confidence"]))
    b.append(box(270, rb, 128, 58, ["Propose a fix", "exact code diff"]))
    b.append(hexagon(118, rb, 144, 62, ["Human approval", "approve / reject"]))
    b.append(arrow(f"M{578 - 64} {rb}H{424 + 66}"))
    b.append(arrow(f"M{424 - 64} {rb}H{270 + 66}"))
    b.append(arrow(f"M{270 - 64} {rb}H{118 + 74}"))
    # row C
    rc = 340
    b.append(arrow(f"M118 {rb + 31}V{rc - 30}", AMBER))
    b.append(label(128, rb + 62, "approve", "start", AMBER, weight=600))
    b.append(box(118, rc, 150, 58, ["Repair", "on ai/repair/* branch"]))
    b.append(box(330, rc, 128, 58, ["Re-test", "real pytest again"]))
    b.append(arrow(f"M{118 + 75} {rc}H{330 - 66}"))
    b.append(box(520, rc, 128, 58, ["Reflection", "what improved?"], "bad"))
    b.append(arrow(f"M{330 + 64} {rc}H{520 - 66}", RED, marker="ar"))
    b.append(label(424, rc - 9, "still failing", color=RED, weight=600))
    b.append(arrow(f"M{520 + 64} {rc}H600V{rb + 31}", RED, dashed=True, marker="ar"))
    b.append(label(610, rc - 30, "try again", "start", RED))
    b.append(label(610, rc - 16, "(max 3 attempts)", "start", RED))
    b.append(box(330, 450, 170, 58, ["Pull request", "opened, never auto-merged"], "good"))
    b.append(arrow(f"M330 {rc + 29}V{450 - 31}", GREEN, marker="ag"))
    b.append(label(340, rc + 58, "all tests pass", "start", GREEN, weight=600))
    return svg(500, "".join(b), "From a commit to a verified fix: the whole flow")


def d_scenario3():
    b = []
    rows = [("Developer's broken commit", 6, RED), ("After fix 1 (restore the / operator)", 8, AMBER),
            ("After fix 2 (restore divide() from main)", 9, GREEN)]
    x0, wmax = 300, 400
    for i, (name, n, c) in enumerate(rows):
        y = 64 + i * 46
        b.append(label(x0 - 12, y + 16, name, "end", INK, 12.5, 600))
        b.append(f"<rect x='{x0}' y='{y}' width='{wmax}' height='24' rx='5' fill='{GRID}'/>")
        b.append(f"<rect x='{x0}' y='{y}' width='{wmax * n / 9:.0f}' height='24' rx='5' fill='{c}' fill-opacity='0.85'/>")
        b.append(label(x0 + wmax * n / 9 - 8, y + 16, f"{n} of 9 tests pass", "end", "#ffffff", 12, 700))
    b.append(label(x0, 212, "Each bar is a real pytest run. Fix 1 was only partly right; reflection noticed the one", "start"))
    b.append(label(x0, 228, "remaining failure (divide by zero) and the system proposed a broader fix 2.", "start"))
    return svg(244, "".join(b), "Scenario 3: two attempts, measured by real tests")


def d_plugin():
    b = []
    b.append(box(380, 78, 340, 54, ["RepairStrategy interface", "investigate()  ·  root_cause()  ·  plan()"], "accent"))
    b.append(box(250, 180, 210, 60, ["Rule-based strategy", "built now - no AI, deterministic"], "good"))
    b.append(box(510, 180, 210, 60, ["LLM agent strategy", "planned - Agentic AI course"], "planned"))
    b.append(arrow("M250 150V107", GREEN, marker="ag"))
    b.append(arrow("M510 150V107", EDGE, dashed=True))
    b.append(label(242, 133, "plugs in today", "end", GREEN, 11, 600))
    b.append(label(518, 133, "plugs in later", "start", QUIET, 11, 600))
    b.append(f"<rect x='40' y='240' width='680' height='96' rx='10' fill='#f5f7fb' stroke='{EDGE}' stroke-width='1.2'/>")
    b.append(label(380, 262, "Shared self-healing loop - stays the same for both", "middle", INK, 13, 650))
    steps = ["Human approval", "Safe repair branch", "Real re-test", "Reflection", "Pull request"]
    for i, s in enumerate(steps):
        cx = 104 + i * 138
        b.append(box(cx, 300, 124, 40, [s], "normal", 12))
        if i:
            b.append(arrow(f"M{cx - 138 + 62} 300H{cx - 63}"))
    b.append(arrow("M380 105V238", BLUE, marker="ab"))
    return svg(352, "".join(b), "Built to grow: AI agents later plug into the same slot")


def d_gate():
    b = []
    b.append(box(90, 120, 130, 56, ["Change pushed", "diff + risk score"]))
    b.append(box(250, 120, 120, 56, ["Run ALL tests", "always"], "accent"))
    b.append(arrow("M155 120H188"))
    b.append(diamond(388, 120, 56, 42, ["Tests", "pass?"]))
    b.append(arrow("M310 120H330"))
    b.append(box(388, 232, 136, 52, ["BLOCK", "red check - fix first"], "bad"))
    b.append(arrow("M388 162V204", RED, marker="ar"))
    b.append(label(398, 186, "no", "start", RED, 11.5, 600))
    b.append(diamond(540, 120, 56, 42, ["Risk", "HIGH?"]))
    b.append(arrow("M444 120H482", GREEN, marker="ag"))
    b.append(label(463, 110, "yes", "middle", GREEN, 11.5, 600))
    b.append(box(540, 232, 136, 52, ["REVIEW", "a human should look"], "human"))
    b.append(arrow("M540 162V204", AMBER))
    b.append(label(550, 186, "yes", "start", AMBER, 11.5, 600))
    b.append(box(686, 120, 116, 52, ["PASS", "good to merge"], "good"))
    b.append(arrow("M596 120H626", GREEN, marker="ag"))
    b.append(label(611, 110, "no", "middle", GREEN, 11.5, 600))
    b.append(label(16, 292, "Risk can only ADD caution (REVIEW). It never skips tests and never turns a failing build green.", "start", QUIET, 11.5))
    return svg(306, "".join(b), "The quality gate: a traffic light for every change")


def d_containers():
    b = []
    b.append(box(78, 120, 110, 60, ["You", "web browser"], "good"))
    b.append(box(256, 120, 170, 84, ["ui container", "Streamlit · port 8502", "pages + buttons only", "no pipeline code"], "accent"))
    b.append(box(486, 120, 190, 84, ["api container", "FastAPI · port 8000", "pipeline · risk model", "self-healing · git · pytest"], "accent"))
    b.append(cylinder(682, 120, 120, 92, ["db container", "PostgreSQL 16", "all records"]))
    b.append(arrow("M133 120H169"))
    b.append(label(151, 110, "HTTP", "middle", QUIET, 10.5))
    b.append(arrow("M341 120H389"))
    b.append(label(365, 110, "REST", "middle", QUIET, 10.5))
    b.append(arrow("M581 120H620"))
    b.append(label(600, 110, "SQL", "middle", QUIET, 10.5))
    b.append(box(486, 236, 190, 44, ["workspace volume", "sandbox repos + branches"], "planned", 12))
    b.append(arrow("M486 162V212"))
    b.append(box(682, 236, 120, 44, ["pgdata volume", "database files"], "planned", 12))
    b.append(arrow("M682 166V212"))
    b.append(label(16, 284, "Start-up order is enforced by health checks:  db healthy  ->  api healthy  ->  ui starts.", "start", QUIET, 11.5))
    b.append(label(16, 300, "The database is only reachable inside the Docker network - not from your computer.", "start", QUIET, 11.5))
    return svg(314, "".join(b), "Three containers, each with one job")


def d_actions():
    b = []
    b.append(box(90, 160, 140, 60, ["git push", "or pull request"], "good"))
    jobs = [
        (70, ["Quality gate (Linux)", "all tests + risk -> PASS/REVIEW/BLOCK", "store tests on PostgreSQL"]),
        (150, ["Tests on Windows", "same suite on another OS"]),
        (230, ["3-container stack", "build ui + api + db, end-to-end run"]),
    ]
    for y, lines in jobs:
        b.append(box(370, y, 260, 64 if len(lines) == 3 else 52, lines, "accent", 12.5))
        b.append(arrow(f"M160 160C200 160 200 {y} 238 {y}"))
    b.append(box(640, 70, 200, 64, ["Self-healing demo", "breaks + fixes scenario 3", "uploads report and PR"], "good", 12.5))
    b.append(arrow("M500 70H538"))
    b.append(label(519, 60, "then", "middle", QUIET, 10.5))
    b.append(label(16, 292, "All four jobs passed on both pushes of the feature branch. A red job would show a red cross on GitHub.", "start", QUIET, 11.5))
    return svg(306, "".join(b), "GitHub Actions: four automatic checks on every push")


def d_risk_levels():
    b = []
    rows = [("LOW", 310, 5.8, GREEN), ("MEDIUM", 297, 14.5, AMBER), ("HIGH", 118, 22.0, RED)]
    x0, scale = 170, 20
    for i, (lvl, n, pct, c) in enumerate(rows):
        y = 60 + i * 44
        b.append(label(x0 - 12, y + 17, f"{lvl}  ({n} commits)", "end", INK, 12.5, 600))
        b.append(f"<rect x='{x0}' y='{y}' width='{pct * scale:.0f}' height='26' rx='5' fill='{c}' fill-opacity='0.85'/>")
        b.append(label(x0 + pct * scale + 8, y + 18, f"{pct}% really failed", "start", INK, 12, 600))
    b.append(f"<line x1='{x0}' y1='54' x2='{x0}' y2='194' stroke='{EDGE}'/>")
    b.append(label(16, 216, "Held-out test commits the model never saw. The higher the predicted level, the more often CI really failed.", "start", QUIET, 11.5))
    return svg(230, "".join(b), "Does a higher risk level mean more failures? Yes")


def d_roadmap():
    b = []
    cols = [(130, "DONE", GREEN, "good"), (380, "NEXT - Agentic AI", BLUE, "accent"), (630, "LATER - DevOps extras", QUIET, "planned")]
    for cx, title, c, _ in cols:
        b.append(f"<rect x='{cx - 118}' y='44' width='236' height='34' rx='8' fill='{c}' fill-opacity='0.14' stroke='{c}' stroke-width='1.4'/>")
        b.append(label(cx, 66, title, "middle", INK, 13, 700))
    done = [["CI simulator + real tests"], ["ML risk model", "3,625 real commits"], ["Self-healing loop", "rule-based, human-approved"],
            ["Quality gate"], ["3 containers", "ui + api + PostgreSQL"], ["GitHub Actions", "Linux, Windows, stack, demo"]]
    nxt = [["RAG retrieval", "search code, tests, docs"], ["LLM Investigation agent"], ["LLM Root-cause agent", "uses RAG evidence"],
           ["LLM Solution planner", "richer, multi-file fixes"], ["Vector database container"], ["Evaluation of agents", "real scenarios, real metrics"]]
    later = [["Real GitHub PR adapter"], ["Push images to a registry", "e.g. GitHub Container Registry"], ["Security scans", "dependencies + images"],
             ["Coverage + lint gates"], ["Monitoring dashboards", "e.g. Prometheus / Grafana"], ["Cloud / Kubernetes deploy"]]
    for cx, items, kind in [(130, done, "good"), (380, nxt, "accent"), (630, later, "planned")]:
        for i, lines in enumerate(items):
            b.append(box(cx, 112 + i * 54, 224, 44, lines, kind, 12))
    b.append(label(16, 436, "LATER items are suggestions for extending the IDT work; none of them is built yet.", "start", QUIET, 11.5))
    return svg(450, "".join(b), "Roadmap: what is built and what comes next")


# ----------------------------------------------------------------------------- document

def fig(svg_code, caption):
    return f"<figure>{svg_code}<figcaption>{esc(caption)}</figcaption></figure>"


CSS = f"""
@page {{ size: A4; margin: 18mm 17mm 18mm 17mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: Inter, -apple-system, 'Helvetica Neue', Arial, sans-serif; color: {INK}; font-size: 10.6pt; line-height: 1.55; margin: 0; }}
h1 {{ font-size: 25pt; margin: 0 0 6px; letter-spacing: -0.02em; }}
h2 {{ font-size: 16.5pt; margin: 26px 0 8px; padding-bottom: 5px; border-bottom: 2px solid {BLUE}; letter-spacing: -0.01em; page-break-after: avoid; }}
h3 {{ font-size: 12.5pt; margin: 18px 0 6px; color: {BLUE}; page-break-after: avoid; }}
p {{ margin: 6px 0 9px; }}
.break {{ page-break-before: always; }}
.keep {{ break-inside: avoid; page-break-inside: avoid; }}
.lead {{ font-size: 12pt; color: #2b3445; }}
.muted {{ color: {QUIET}; }}
table {{ width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 9.6pt; page-break-inside: avoid; }}
th {{ background: #eef3fa; text-align: left; padding: 6px 8px; border-bottom: 1.5px solid #c9d6ea; }}
td {{ padding: 6px 8px; border-bottom: 1px solid {GRID}; vertical-align: top; }}
code {{ font-family: 'JetBrains Mono', Menlo, monospace; font-size: 8.9pt; background: #f2f5f9; padding: 1px 4px; border-radius: 4px; }}
pre {{ font-family: 'JetBrains Mono', Menlo, monospace; font-size: 8.8pt; background: #f4f6fa; border: 1px solid {GRID}; border-radius: 8px; padding: 10px 12px; white-space: pre-wrap; page-break-inside: avoid; margin: 6px 0 12px; }}
figure {{ margin: 12px 0 16px; padding: 10px 10px 6px; border: 1px solid {GRID}; border-radius: 10px; page-break-inside: avoid; }}
figure svg {{ width: 100%; height: auto; display: block; }}
figcaption {{ font-size: 9pt; color: {QUIET}; margin-top: 4px; }}
.simple {{ background: #f0f6ff; border-left: 4px solid {BLUE}; border-radius: 6px; padding: 8px 12px; margin: 10px 0 12px; page-break-inside: avoid; }}
.simple b:first-child {{ color: {BLUE}; }}
.warn {{ background: #fff7e6; border-left: 4px solid {AMBER}; border-radius: 6px; padding: 8px 12px; margin: 10px 0 12px; page-break-inside: avoid; }}
.step {{ border: 1px solid {GRID}; border-radius: 10px; padding: 8px 12px 6px; margin: 10px 0; page-break-inside: avoid; }}
.step h4 {{ margin: 0 0 4px; font-size: 11.5pt; }}
.num {{ display: inline-block; width: 22px; height: 22px; border-radius: 6px; background: {BLUE}; color: #fff; text-align: center; font-size: 10pt; line-height: 22px; margin-right: 6px; }}
.cover {{ height: 245mm; display: flex; flex-direction: column; justify-content: center; }}
.cover .tag {{ display: inline-block; font-size: 9.5pt; font-weight: 600; color: {BLUE}; background: #eaf2fc; border: 1px solid #c9dcf5; border-radius: 999px; padding: 3px 10px; margin: 0 6px 6px 0; }}
.toc li {{ margin: 3px 0; }}
.grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
.card {{ border: 1px solid {GRID}; border-radius: 10px; padding: 8px 12px; page-break-inside: avoid; }}
.card h4 {{ margin: 2px 0 4px; }}
"""


def build_html() -> str:
    P = []
    a = P.append

    # ---------------------------------------------------------------- cover
    a(f"""<section class='cover'>
<div class='muted' style='font-weight:600;letter-spacing:.08em'>SEMESTER 7 PROJECT · PROJECT GUIDE</div>
<h1 style='font-size:32pt;margin-top:10px'>SEM7 Self-Healing CI/CD</h1>
<p class='lead' style='font-size:14pt;max-width:150mm'>An ML-assisted DevOps pipeline that predicts risky code changes, catches failing tests,
proposes a fix, and repairs the code only after a human approves - then proves the fix with real tests.</p>
<div style='margin:14px 0 22px'><span class='tag'>Agentic AI course</span><span class='tag'>IDT / DevOps course</span>
<span class='tag'>Python · FastAPI · Streamlit · PostgreSQL · Docker · GitHub Actions</span></div>
<table style='max-width:150mm'>
<tr><th>Status as of</th><td>2 October 2026</td></tr>
<tr><th>Repository</th><td>github.com/Lakshay0916/SEM7 (private)</td></tr>
<tr><th>Code described here</th><td>branch <code>feature/devops-self-healing</code> (commit <code>ac0375e</code>); <code>main</code> holds the earlier version</td></tr>
<tr><th>Automated tests</th><td>84 passing locally (+5 PostgreSQL-only), 81 passing inside Docker on PostgreSQL</td></tr>
<tr><th>CI status</th><td>All 4 GitHub Actions jobs green</td></tr>
</table>
<h3 style='margin-top:18px'>Contents</h3>
<ol class='toc'>
<li>The project in one minute</li><li>The big picture</li><li>What happens when code changes (steps 1-5)</li>
<li>The ML risk model</li><li>The self-healing loop (step 6)</li><li>Safety rules</li>
<li><b>The IDT / DevOps part</b></li><li>Proof that it works</li><li>How to run it yourself</li>
<li>What comes next</li><li>Glossary</li></ol>
</section>""")

    # ---------------------------------------------------------------- 1
    a("""<h2 class='break'>1. The project in one minute</h2>
<p class='lead'>Software teams constantly push code changes. Sometimes a change breaks something, the automated tests fail,
and a developer has to stop, read the error logs, find the cause, fix it, and test again. This project automates
as much of that as is safe - while keeping a human in control.</p>
<div class='simple'><b>In simple words:</b> think of it as a careful assistant for your code. It looks at every change,
warns you if it looks risky, runs the tests for real, and if something breaks it explains why and suggests a fix.
It only touches the code after you say "yes", it never touches the main version, and it proves its fix by running the tests again.</div>
<p>The same codebase serves two courses:</p>
<table>
<tr><th style='width:22%'>Course</th><th>Focus</th><th style='width:24%'>Status</th></tr>
<tr><td><b>IDT / DevOps</b></td><td>Quality gate, real CI tests, a human-approved self-healing loop, Docker (3 containers),
GitHub Actions, reports</td><td><b>Implemented and tested</b></td></tr>
<tr><td><b>Agentic AI</b></td><td>AI agents (using a large language model) and RAG (searching project knowledge) to investigate failures
and plan smarter fixes</td><td>Next phase - the code already has the slot they plug into</td></tr>
</table>
<p>One phrase sums up how the pieces divide the work: <b>"ML predicts, RAG retrieves, LLM reasons, agents act, and tests verify."</b>
Today, the "reasoning" part uses simple, transparent rules instead of an AI model. Everything else around it is real.</p>""")

    # ---------------------------------------------------------------- 2
    a("<div class='keep'><h2>2. The big picture</h2><p>Every change travels through the same path. The top row runs on every change. "
      "If the tests fail, the lower rows - the self-healing loop - take over.</p>"
      + fig(d_big_picture(), "Figure 1. The full flow. Orange hexagon = the human decision point. Red dashed line = retry after a failed fix.")
      + "</div>")
    a("""<div class='simple'><b>Key idea:</b> the system can investigate and suggest on its own, but changing code requires a person's approval,
and "fixed" is decided only by real test results - never by the system's own opinion.</div>""")

    # ---------------------------------------------------------------- 3
    a("<h2>3. What happens when code changes (steps 1-5)</h2>"
      "<p>To demonstrate this safely, the project uses a small <b>calculator</b> program (add, subtract, multiply, divide, average) with "
      "9 tests. Each <b>scenario</b> deliberately breaks it in a known way. Example used below - <b>Scenario 1</b>: someone changes "
      "<code>add(a, b)</code> to return <code>a - b</code>.</p>")
    steps = [
        ("Create a safe copy and the developer's commit",
         "The calculator is copied into a brand-new, separate git repository. The healthy code is saved on the <code>main</code> branch. "
         "Then the scenario's change is saved (committed) on a separate <b>feature branch</b>, exactly like a developer would do.",
         "Nothing real is ever at risk, every run starts from the same point, and <code>main</code> always stays healthy as a reference.",
         "Feature branch <code>feature/refactor-add</code>, commit message \"Refactor add() helper\"."),
        ("Work out exactly what changed",
         "The feature branch is compared with <code>main</code> (a <b>diff</b>). Each changed file is classified (source code, test, settings, "
         "dependency list), and the changed lines are mapped to the exact <b>functions</b> they belong to.",
         "The next steps need to know what changed. A failing <code>test_add</code> right after <code>add()</code> changed is strong evidence.",
         "1 file changed, 1 line added, 1 removed, function changed: <code>add</code>."),
        ("Predict how risky the change is (ML)",
         "The change is turned into 27 numbers (how big it is, how many files, were tests touched, etc.). A machine-learning model trained on "
         "3,625 real commits from 9 open-source projects gives a risk score from 0 to 1 and a level: LOW, MEDIUM or HIGH - plus the reasons.",
         "An early warning before tests run, useful for deciding what to review carefully. It is a hint, not proof.",
         "Risk HIGH (score 0.578). Reason: \"source changed without accompanying test changes\"."),
        ("Run the real tests",
         "<code>pytest</code> (the Python testing tool) is actually executed, with a time limit. The result is read from pytest's own "
         "machine-readable report. The verdict is PASS only if pytest finished normally, at least one test ran, and nothing failed.",
         "Tests are the ground truth. A timeout, crash or \"no tests found\" can never be reported as a pass.",
         "FAIL: 7 passed, 2 failed. <code>test_add</code>: expected 5 but got -1."),
        ("Package the evidence",
         "If tests failed, everything known is bundled into one structured record (a <b>FailureEvent</b>): the commit, branch, failing tests, "
         "error messages, logs and the diff. Every step is also saved in the database with a timestamp.",
         "The next stage works only from these facts, so it cannot invent logs or results - and anyone can audit the history later.",
         "2 failing tests recorded, workflow state = FAILED."),
    ]
    for i, (title, what, why, ex) in enumerate(steps, 1):
        a(f"""<div class='step'><h4><span class='num'>{i}</span>{title}</h4>
<p><b>What happens:</b> {what}</p><p><b>Why it matters:</b> {why}</p><p class='muted'><b>Scenario 1 result:</b> {ex}</p></div>""")
    a("""<p>The three demo scenarios and what they show:</p><table>
<tr><th>Scenario</th><th>What is broken</th><th>Risk</th><th>Tests</th><th>Shows</th></tr>
<tr><td>1 - Simple bug</td><td><code>add()</code> subtracts instead of adding</td><td>HIGH (0.578)</td><td>FAIL 7/9</td><td>One small fix repairs it</td></tr>
<tr><td>2 - Higher-risk change</td><td>Nothing - a broad change (4 files, new dependency, security module)</td><td>MEDIUM (0.314)</td><td>PASS 9/9</td><td>Risk is not the same as a bug</td></tr>
<tr><td>3 - Failed first repair</td><td><code>divide()</code> uses floor division AND lost its zero check</td><td>HIGH (0.443)</td><td>FAIL 6/9</td><td>First fix is not enough; the system retries</td></tr>
</table>""")

    # ---------------------------------------------------------------- 4
    a("""<h2>4. The ML risk model</h2>
<p class='lead'>The model answers one question: <b>"How likely is this kind of change to break the tests?"</b> It does not find the bug.</p>
<h3>Where the training data came from</h3>
<p>There was no history for the demo project, so real history was collected (<b>mined</b>) from 9 well-known open-source Python projects
on GitHub: Flask, Click, httpx, Rich, Requests, Alembic, attrs, pip-tools and Poetry. For each commit, the project's real automated test result
(GitHub Actions) was downloaded and the commit's changes were measured.</p>
<table><tr><th>What was collected</th><th>Result</th></tr>
<tr><td>Commits with a clear test result</td><td>3,625</td></tr>
<tr><td>Commits whose tests really failed</td><td>323 (8.9%)</td></tr>
<tr><td>Projects</td><td>9 (largest: Click 934 commits, Poetry 861; smallest: Rich 83)</td></tr></table>
<div class='warn'><b>Cleaning the labels mattered.</b> Flask runs one test job against unreleased versions of its dependencies; that job fails no matter
what the commit does. Counting it made Flask look 69% broken; ignoring it gives the true 23.5%. Runs that failed before any step even started
(server problems, not code problems) were also dropped.</div>
<h3>How it was trained fairly</h3>
<ul>
<li><b>Time-ordered split:</b> for each project, the oldest 70% of commits trained the model, the next 10% chose settings, and the newest 20%
tested it - like predicting the future from the past.</li>
<li><b>No peeking:</b> "history" features (e.g. "did the previous commit fail?") only use results that were already known at that moment. An automated test checks this.</li>
<li><b>Compared with simple rules:</b> the model only counts as useful if it beats rules like "always predict pass" or "the previous commit failed".</li>
</ul>
<h3>Real results (on the newest 20% of commits, never seen in training)</h3>
<table><tr><th>Model</th><th>ROC-AUC</th><th>PR-AUC</th><th>Plain meaning</th></tr>
<tr><td><b>Full model</b> (change + CI history)</td><td>0.663</td><td>0.232</td><td>Clearly better than chance; beats the best simple rule</td></tr>
<tr><td><b>Diff-only model</b> (used for the demo)</td><td>0.604</td><td>0.161</td><td>Weaker - a change alone says only a little</td></tr>
<tr><td>Simple rule: "previous commit failed"</td><td>0.572</td><td>0.170</td><td>The bar the model must beat</td></tr>
<tr><td>Always guess "pass"</td><td>0.500</td><td>0.120</td><td>Pure chance</td></tr></table>""")
    a(fig(d_risk_levels(), "Figure 2. Full model on held-out commits: LOW 5.8%, MEDIUM 14.5%, HIGH 22.0% actually failed."))
    a("""<div class='simple'><b>Honest takeaway:</b> the risk model gives a real but modest signal. That is why the system treats risk as a reason
for extra caution, never as a replacement for running the tests (see the quality gate in section 7).</div>""")

    # ---------------------------------------------------------------- 5
    a("""<h2>5. The self-healing loop (step 6)</h2>
<p class='lead'>When tests fail, the system tries to repair the code - carefully, in small steps, with a person approving every change.</p>
<table><tr><th style='width:23%'>Stage</th><th>What it does (Scenario 1 example)</th></tr>
<tr><td><b>Investigate</b></td><td>Lists the facts: which tests failed and with what error; which functions the commit changed. Guesses are labelled
as <i>hypotheses</i>, never mixed with facts. <span class='muted'>"test_add failed: expected 5, got -1" · "commit changed add()"</span></td></tr>
<tr><td><b>Root cause</b></td><td>Explains why, with a confidence level. <span class='muted'>"The commit changed the operator in add() from + to -" (80% confidence)</span></td></tr>
<tr><td><b>Propose a fix</b></td><td>Shows the exact change as a red/green diff, the tests to run, the risks of the fix and how to undo it.
Nothing has been changed yet.</td></tr>
<tr><td><b>Human approval</b></td><td>You click Approve or Reject. Rejecting changes nothing; you can ask it to re-analyse or stop.</td></tr>
<tr><td><b>Repair</b></td><td>Applies only the approved change, on a new branch <code>ai/repair/&lt;id&gt;</code> - never on <code>main</code> or the developer's branch.</td></tr>
<tr><td><b>Re-test</b></td><td>Runs the full test suite again for real. <span class='muted'>9 of 9 pass.</span></td></tr>
<tr><td><b>Reflection</b></td><td>If tests still fail: records what improved, what is still broken and whether anything new broke, then proposes a better fix
(which needs approval again). Maximum 3 attempts.</td></tr>
<tr><td><b>Pull request</b></td><td>When tests pass, a pull request description is written from the recorded evidence: problem, cause, fix, tests, approvals.
It is never merged automatically.</td></tr></table>
<h3>How the fixes are chosen today (rule-based, no AI)</h3>
<p>The current strategy is deliberately simple and explainable. It always tries the <b>smallest</b> fix first:</p>
<ol><li><b>Restore the operator:</b> if a changed line differs from the original only by an operator (<code>+</code> to <code>-</code>, <code>/</code> to <code>//</code>), put the original operator back.</li>
<li><b>Restore the function:</b> if tests still fail, restore the whole affected function to its healthy version from <code>main</code>.</li>
<li>If neither applies, it stops and hands over to a human.</li></ol>""")
    a(fig(d_scenario3(), "Figure 3. Scenario 3 measured by real test runs: 6/9 -> 8/9 after fix 1 -> 9/9 after fix 2, then a pull request."))
    a(fig(d_plugin(), "Figure 4. The fixing 'brain' is a replaceable part. Today it is rule-based; the Agentic AI course will plug AI agents into the same slot."))

    # ---------------------------------------------------------------- 6
    a("""<h2>6. Safety rules</h2>
<p>These are enforced in code and checked by automated tests - not just promised.</p>
<table><tr><th style='width:34%'>The system never...</th><th>How it is enforced</th></tr>
<tr><td>changes <code>main</code></td><td>When the AI side uses git, any commit, reset or push to <code>main</code>/<code>master</code> is refused. Its branches must start with <code>ai/repair/</code>. There is a live "try it" button in the dashboard.</td></tr>
<tr><td>changes code without approval</td><td>A state machine only allows the step "approved -> repairing". Skipping approval is impossible.</td></tr>
<tr><td>edits the tests to make them pass</td><td>Repairs may not touch test files, settings or dependency files.</td></tr>
<tr><td>makes an unclear edit</td><td>The text to replace must appear exactly once in the file.</td></tr>
<tr><td>claims a pass without proof</td><td>PASS only comes from a real pytest run; timeouts and crashes are reported as such.</td></tr>
<tr><td>retries forever</td><td>Maximum 3 repair attempts, then it stops and asks a human.</td></tr>
<tr><td>merges a pull request</td><td>Pull requests are only opened, never merged.</td></tr>
<tr><td>leaks secrets</td><td>Passwords/tokens come only from environment settings and are hidden in logs and the database.</td></tr></table>""")

    # ---------------------------------------------------------------- 7 IDT
    a("""<h2 class='break'>7. The IDT / DevOps part</h2>
<p class='lead'>DevOps is about getting code from a developer to users quickly and safely: automated testing, consistent environments,
automatic checks on every change, and clear records. This section explains everything built for the IDT course.</p>
<table><tr><th style='width:30%'>Before the IDT work</th><th>After the IDT work</th></tr>
<tr><td>Pipeline stopped at "tests failed"</td><td>Self-healing loop: investigate -> approve -> repair -> re-test -> pull request</td></tr>
<tr><td>Risk score shown, but not used</td><td>Risk-aware <b>quality gate</b>: PASS / REVIEW / BLOCK</td></tr>
<tr><td>Ran only on one laptop</td><td><b>Docker</b>: 3 containers that run the same on any computer</td></tr>
<tr><td>Everything in one program</td><td>Separate <b>UI</b>, <b>API</b> and <b>PostgreSQL database</b></td></tr>
<tr><td>Tests run by hand</td><td><b>GitHub Actions</b>: 4 automatic checks on every push</td></tr>
<tr><td>Only tested on macOS</td><td>Also verified on <b>Windows</b> and inside Linux containers</td></tr>
<tr><td>Results only on screen</td><td>Exportable <b>reports</b> and pull-request descriptions</td></tr></table>
<h3>7.1 The quality gate</h3>
<p>A <b>quality gate</b> is an automatic checkpoint that decides whether a change is good enough to continue. Ours combines the real test result
with the ML risk score:</p>""")
    a(fig(d_gate(), "Figure 5. The quality gate. Tests always run first; risk can only add a review flag."))
    a("""<p>On GitHub this shows up as a green tick (PASS), a yellow warning (REVIEW) or a red cross (BLOCK), and a full report appears in the
run summary. Example from the real push of this branch: <b>PASS</b> - 75 tests passed, risk MEDIUM (0.317).</p>
<div class='simple'><b>Why not skip tests for low-risk changes?</b> The original plan considered sending HIGH-risk changes straight to review without
testing. We chose not to: the demo's risk model is only modest (ROC-AUC 0.60), so skipping tests based on it would let real bugs through.</div>""")

    a("<div class='keep'><h3>7.2 Three containers: UI + API + PostgreSQL</h3>"
      "<p>A <b>container</b> is a sealed box that holds a program together with everything it needs to run, so it behaves the same on every "
      "computer. The project is split into three containers, each with one job:</p>"
      + fig(d_containers(), "Figure 6. The three containers and how they talk to each other.") + "</div>")
    a("""<table><tr><th style='width:16%'>Container</th><th style='width:24%'>Technology</th><th>Its job</th></tr>
<tr><td><b>ui</b></td><td>Streamlit (Python web UI)</td><td>Shows the dashboard and sends your button clicks to the API. It contains <b>no pipeline code</b> -
which proves the front end and back end are truly separate. Image size: 824 MB.</td></tr>
<tr><td><b>api</b></td><td>FastAPI (Python web API)</td><td>Does all the work: runs pipelines, the risk model and the self-healing loop, with git and pytest inside.
Has interactive documentation at <code>/docs</code>. Image size: 1.01 GB.</td></tr>
<tr><td><b>db</b></td><td>PostgreSQL 16</td><td>Stores every workflow, event and artifact permanently. Not reachable from outside the Docker network.</td></tr></table>
<h3>7.3 How Docker Compose runs them</h3>
<p><b>Docker Compose</b> starts all three containers with one command and wires them together. Important details:</p>
<ul>
<li><b>Health checks and start-up order:</b> each container reports whether it is healthy. The API waits for a healthy database; the UI waits for a healthy API.</li>
<li><b>Volumes (permanent storage):</b> <code>pgdata</code> keeps the database and <code>workspace</code> keeps the sandbox repositories, so data survives restarts.</li>
<li><b>Configuration from a <code>.env</code> file:</b> database user and password, and the ports - so nothing secret is hard-coded.</li>
<li><b>Configurable ports:</b> if 8000 or 8502 is busy, run <code>API_PORT=8010 UI_PORT=8512 docker compose up</code>.</li>
<li><b>Helper services:</b> <code>tests</code> runs the test suite inside the API image against PostgreSQL; <code>cli</code> runs command-line scripts.</li>
</ul>
<h3>7.4 The API (the backend's front door)</h3>
<table><tr><th style='width:42%'>Endpoint</th><th>What it does</th></tr>
<tr><td><code>GET /health</code></td><td>Is the API up, which database is used, which risk model is loaded</td></tr>
<tr><td><code>GET /scenarios</code></td><td>List the demo scenarios</td></tr>
<tr><td><code>POST /workflows</code></td><td>Run a scenario: commit, diff, risk, tests, and propose a fix if it fails</td></tr>
<tr><td><code>GET /workflows</code>, <code>GET /workflows/{id}</code></td><td>List runs / get the full history of one run</td></tr>
<tr><td><code>POST /workflows/{id}/approve</code></td><td>Approve the proposed fix: repair, re-test, then pull request or reflection</td></tr>
<tr><td><code>POST /workflows/{id}/reject</code>, <code>/abort</code>, <code>/propose</code></td><td>Reject, stop, or re-analyse</td></tr>
<tr><td><code>POST /workflows/{id}/safety-check</code></td><td>Try an AI commit to main - must be blocked</td></tr>
<tr><td><code>GET /risk/model</code>, <code>GET /risk/evaluation</code></td><td>Risk model details and its evaluation results</td></tr></table>
<p class='muted'>Trying to approve when nothing is waiting for approval returns an error (HTTP 409) - the "no repair without approval" rule holds over the API too.</p>""")

    a("<div class='keep'><h3>7.5 GitHub Actions (automatic checks on every push)</h3>"
      "<p><b>GitHub Actions</b> is GitHub's built-in automation: every time code is pushed, GitHub's own servers run the checks below and show "
      "the result next to the commit.</p>"
      + fig(d_actions(), "Figure 7. The four CI jobs. Three run in parallel; the demo runs after the quality gate passes.") + "</div>")
    a("""<table><tr><th style='width:26%'>Job</th><th>What it proves</th><th style='width:12%'>Result</th></tr>
<tr><td>Quality gate (Linux)</td><td>All tests pass, the gate gives its verdict, and the database code also works on a real PostgreSQL server</td><td>Passed</td></tr>
<tr><td>Tests (Windows)</td><td>The project works on Windows too (a teammate uses Windows)</td><td>Passed</td></tr>
<tr><td>3-container stack</td><td>Builds ui + api + db, starts them in order, runs Scenario 3 end to end through the API, runs tests in the container</td><td>Passed</td></tr>
<tr><td>Self-healing demo</td><td>Breaks and repairs Scenario 3 automatically; the report and pull request can be downloaded from the run page</td><td>Passed</td></tr></table>
<h3>7.6 Working on every operating system</h3>
<p>Making the Windows job pass meant fixing three classic cross-platform problems: file paths (Windows uses <code>\\</code> instead of <code>/</code>),
text encoding (Windows does not default to UTF-8, which breaks symbols like arrows), and line endings (Windows adds an extra character at the end of each line).</p>
<h3>7.7 Reports</h3>
<ul><li><b>Workflow report:</b> any run can be exported as a readable Markdown report - what broke, why, each attempt, who approved, test results, pull request.</li>
<li><b>Pull-request description:</b> written automatically from recorded evidence only.</li>
<li><b>CI gate summary:</b> shown on every GitHub Actions run.</li></ul>
<h3>7.8 The dashboard</h3>
<p>The Streamlit dashboard explains every step on screen ("what happens" and "why it matters"), shows the risk gauge, the real test output,
the proposed fix as a diff with <b>Approve / Reject</b> buttons, each repair attempt, reflection notes and the final pull request.
It supports light and dark mode and has pages for the dashboard, how it works, running a pipeline, workflow history and the risk model.</p>""")

    # ---------------------------------------------------------------- 8
    a("""<h2>8. Proof that it works</h2>
<table><tr><th style='width:42%'>Check</th><th>Result</th></tr>
<tr><td>Automated tests on macOS</td><td>84 passed (+5 that run only with PostgreSQL)</td></tr>
<tr><td>Automated tests inside the API container, on PostgreSQL</td><td>81 passed</td></tr>
<tr><td>GitHub Actions - first push of the branch</td><td>4 of 4 jobs passed</td></tr>
<tr><td>GitHub Actions - after the 3-container change</td><td>4 of 4 jobs passed</td></tr>
<tr><td>End-to-end check through the API (Docker)</td><td>Scenario 3: FAIL -> reflect -> PASS -> pull request; AI write to main blocked</td></tr>
<tr><td>Browser test of the UI container</td><td>Scenario 1 run and approved; repaired and re-tested 9/9</td></tr>
<tr><td>Data stored in PostgreSQL for one Scenario 3 run</td><td>27 events and 18 artifacts</td></tr></table>
<p>What the tests cover, in plain terms:</p>
<ul><li><b>Safety:</b> AI cannot touch main; no repair without approval; tests and settings cannot be edited; retries stop at 3; rejections change nothing.</li>
<li><b>Honest results:</b> timeouts, crashes and empty test runs are never reported as passes.</li>
<li><b>ML fairness:</b> history features never use information from the future.</li>
<li><b>The whole loop:</b> Scenario 1 fixed in one attempt; Scenario 3 needs two attempts with reflection in between.</li>
<li><b>The API and UI:</b> every endpoint, error cases, and the UI showing a clear message when the API is down.</li></ul>""")

    # ---------------------------------------------------------------- 9
    a("""<h2>9. How to run it yourself</h2>
<h3>Option A - Docker (recommended, no Python setup)</h3>
<pre>docker compose up --build
# UI:  http://localhost:8502      API docs: http://localhost:8000/docs
# ports busy?  API_PORT=8010 UI_PORT=8512 docker compose up --build</pre>
<p>Then in the UI: <b>Run Pipeline</b> -> choose <b>Scenario 3</b> -> <b>Run the pipeline</b> -> scroll to step 6 -> <b>Approve &amp; apply</b> (twice).</p>
<pre>docker compose --profile tools run --rm tests             # run the tests inside Docker
python scripts/e2e_api.py --url http://localhost:8000     # end-to-end check of the running stack
docker compose down                                        # stop (add -v to delete the data)</pre>
<h3>Option B - Without Docker (two terminals)</h3>
<pre>conda activate SEM7
uvicorn app.api.main:app --port 8000      # terminal 1: the backend
streamlit run ui/app.py                   # terminal 2: the dashboard</pre>
<h3>Option C - Command line only</h3>
<pre>python scripts/run_ci.py --scenario failed_first_repair --heal --approve
python scripts/export_report.py --latest
python scripts/ci_gate.py --base main
pytest</pre>""")

    # ---------------------------------------------------------------- 10
    a("""<div class='keep'><h2>10. What comes next</h2>
<p class='lead'>The foundation - pipeline, safety rules, approval, testing, containers and CI - is finished. The next work replaces the
simple rule-based "brain" with AI agents, and can optionally extend the DevOps side further.</p>"""
      + fig(d_roadmap(), "Figure 8. Roadmap. Green = built, blue = next (Agentic AI), grey dashed = possible DevOps extensions.") + "</div>")
    a("""<h3>Agentic AI (next)</h3>
<table><tr><th style='width:30%'>To add</th><th>What it will do</th></tr>
<tr><td><b>RAG retrieval</b></td><td>Search the project's code, tests, documentation and history for the pieces relevant to a failure, and pass them to the agents as evidence.</td></tr>
<tr><td><b>LLM Investigation agent</b></td><td>Read logs and diffs like a developer would, and summarise what failed - still separating facts from guesses.</td></tr>
<tr><td><b>LLM Root-cause agent</b></td><td>Explain why the failure happened using the retrieved evidence, with a confidence level.</td></tr>
<tr><td><b>LLM Solution planner</b></td><td>Propose fixes beyond reverting a change (new logic, several files) - still shown as a diff for approval.</td></tr>
<tr><td><b>Vector database container</b></td><td>A fourth container to store the searchable knowledge used by RAG.</td></tr>
<tr><td><b>Agent evaluation</b></td><td>Measure root-cause correctness, fix success rate and attempts needed on real scenarios.</td></tr></table>
<div class='simple'><b>Why this will be easy to add:</b> the three "thinking" steps already sit behind one interface
(<code>investigate</code>, <code>root_cause</code>, <code>plan</code>). An AI version only has to fill those three slots. The approval step,
safety rules, real re-testing, reflection, pull request, dashboard and database records are reused unchanged.</div>
<h3>Possible DevOps extensions (later)</h3>
<p>These are suggestions to strengthen the IDT side further; none is built yet:</p>
<ul><li><b>Real GitHub pull requests</b> instead of the local adapter used for the demo repositories.</li>
<li><b>Container registry:</b> publish the images automatically (e.g. GitHub Container Registry) after a green build.</li>
<li><b>Security scanning</b> of dependencies and container images.</li>
<li><b>Coverage and lint gates:</b> also block changes that reduce test coverage or break code style.</li>
<li><b>Monitoring:</b> dashboards for pipeline runs, repair success rate and timings (e.g. Prometheus and Grafana).</li>
<li><b>Cloud or Kubernetes deployment</b> of the three containers.</li></ul>""")

    # ---------------------------------------------------------------- 11
    a("""<h2>11. Glossary</h2>
<table><tr><th style='width:24%'>Term</th><th>Meaning in plain English</th></tr>
<tr><td>Commit</td><td>A saved snapshot of code changes, with a message describing them.</td></tr>
<tr><td>Branch</td><td>A separate line of work. <code>main</code> is the official version; feature and repair branches hold changes until approved.</td></tr>
<tr><td>Diff</td><td>The list of exactly what changed between two versions (removed lines in red, added lines in green).</td></tr>
<tr><td>Pull request (PR)</td><td>A request to merge a branch into another, so people can review it first.</td></tr>
<tr><td>CI (Continuous Integration)</td><td>Automatically testing every change as soon as it is pushed.</td></tr>
<tr><td>CI/CD</td><td>CI plus Continuous Delivery/Deployment - automatically getting tested code out to users.</td></tr>
<tr><td>pytest</td><td>The standard tool for running automated tests in Python.</td></tr>
<tr><td>Quality gate</td><td>An automatic checkpoint that decides whether a change may continue (PASS / REVIEW / BLOCK here).</td></tr>
<tr><td>Machine learning model</td><td>A program that learns patterns from past examples to make predictions about new ones.</td></tr>
<tr><td>ROC-AUC</td><td>How well a model ranks risky changes above safe ones. 0.5 = coin flip, 1.0 = perfect.</td></tr>
<tr><td>PR-AUC</td><td>A stricter score for rare events like failures. Blind guessing scores the failure rate (about 0.12 here).</td></tr>
<tr><td>Container / Docker</td><td>A sealed box with a program and everything it needs, so it runs the same everywhere. Docker builds and runs them.</td></tr>
<tr><td>Docker Compose</td><td>A tool that starts several containers together from one file.</td></tr>
<tr><td>Volume</td><td>Storage that lives outside a container, so data survives restarts.</td></tr>
<tr><td>Health check</td><td>A regular "are you OK?" test a container answers; used to start services in the right order.</td></tr>
<tr><td>API</td><td>A set of web addresses a program offers so other programs (like the UI) can ask it to do things.</td></tr>
<tr><td>FastAPI / Streamlit</td><td>Python frameworks for building web APIs and web dashboards.</td></tr>
<tr><td>PostgreSQL</td><td>A widely used, reliable database server.</td></tr>
<tr><td>GitHub Actions</td><td>GitHub's automation that runs jobs (tests, builds) on GitHub's servers after each push.</td></tr>
<tr><td>LLM</td><td>Large Language Model - an AI model (like Claude) that understands and writes text and code.</td></tr>
<tr><td>Agent</td><td>An AI component given one job (e.g. "find the root cause") and the tools to do it.</td></tr>
<tr><td>RAG</td><td>Retrieval-Augmented Generation - first search for relevant information, then give it to the LLM as evidence.</td></tr>
<tr><td>State machine</td><td>A fixed list of allowed steps and moves between them (e.g. you can only repair after approval).</td></tr></table>
<p class='muted' style='margin-top:18px'>All numbers in this guide come from real runs of the code on the branch named on the cover
(test runs, GitHub Actions runs and the saved model evaluation).</p>""")

    return f"<!doctype html><html><head><meta charset='utf-8'><title>SEM7 Self-Healing CI/CD - Project Guide</title><style>{CSS}</style></head><body>{''.join(P)}</body></html>"


def main() -> None:
    OUT_HTML.write_text(build_html(), encoding="utf-8")
    OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page()
        page.goto(OUT_HTML.as_uri())
        page.wait_for_timeout(500)
        page.pdf(
            path=str(OUT_PDF), format="A4", print_background=True, prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=("<div style='font-size:8px;color:#8a93a3;width:100%;padding:0 17mm;display:flex;"
                             "justify-content:space-between;font-family:Helvetica,Arial'><span>SEM7 Self-Healing CI/CD - Project Guide</span>"
                             "<span><span class='pageNumber'></span> / <span class='totalPages'></span></span></div>"),
        )
        b.close()
    print(f"PDF -> {OUT_PDF}")


if __name__ == "__main__":
    main()
