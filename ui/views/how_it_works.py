import streamlit as st

from ui.content import STEPS, UPCOMING
from ui.style import hero, is_dark

_STYLE = """
  rankdir=LR; bgcolor="transparent"; nodesep=0.3; ranksep=0.35;
  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, margin="0.2,0.1",
        color="#9fbfe6", fillcolor="#eaf2fc", fontcolor="#1d2433"];
  edge [color="#8a93a3", arrowsize=0.7, fontname="Helvetica", fontsize=10, fontcolor="#6b7588"];
"""

BUILT = "digraph A {" + _STYLE + """
  commit [label="Developer\\ncommit"]; diff [label="Diff\\nextraction"]; risk [label="ML risk\\nmodel"];
  ci [label="CI tests\\n(real pytest)"];
  pass [label="PASS\\nnothing to fix", fillcolor="#e6f5e6", color="#9fd59f"];
  fail [label="FailureEvent\\n(evidence)", fillcolor="#fdeaea", color="#ec9a9a"];
  commit -> diff -> risk -> ci; ci -> pass [label="pass"]; ci -> fail [label="fail"];
}"""

AGENTS = "digraph B {" + _STYLE.replace("rankdir=LR", "rankdir=TB; newrank=true") + """
  node [style="rounded,dashed", fillcolor="#fafbfc", color="#b4bcc9", fontcolor="#5b6475"];
  fail [label="FailureEvent", style="rounded,filled", fillcolor="#fdeaea", color="#ec9a9a", fontcolor="#1d2433"];
  inv [label="Investigation\\nagent"]; rag [label="RAG\\nretrieval"]; rc [label="Root cause\\nagent"];
  plan [label="Solution\\nplanner"]; appr [label="Human\\napproval", shape=hexagon];
  rep [label="Repair agent\\nai/repair/*"]; test [label="Testing\\nagent"]; pr [label="Pull\\nrequest"];
  refl [label="Reflection"];
  { rank=same; fail; inv; rag; rc; plan; }
  { rank=same; appr; rep; test; pr; }
  fail -> inv -> rag -> rc -> plan;
  plan -> appr; appr -> rep [label="approve"]; rep -> test; test -> pr [label="pass"];
  test -> refl [label="fail"]; refl -> inv [style=dashed, label="retry (max 3)"];
  appr -> inv [style=dashed, label="reject"];
}"""


_DARK = {  # light-diagram color -> dark-diagram color
    "#eaf2fc": "#1a2a42", "#9fbfe6": "#3d6aa8", "#1d2433": "#e6eaf0",
    "#e6f5e6": "#173524", "#9fd59f": "#2f8a52", "#fdeaea": "#3d1d22", "#ec9a9a": "#b54a4a",
    "#fafbfc": "#141a24", "#b4bcc9": "#4c586b", "#5b6475": "#a9b3c2", "#8a93a3": "#7d889a", "#6b7588": "#97a1b1",
}


def _themed(dot: str) -> str:
    if not is_dark():
        return dot
    for light, dark in _DARK.items():
        dot = dot.replace(light, dark)
    return dot


def render() -> None:
    st.title("How it works")
    hero(
        "One pipeline, clear responsibilities, a human in the loop",
        "A commit flows left to right. Solid boxes are built and running today; dashed boxes are the AI-agent "
        "stages planned next. The hexagon is the human approval gate - no code is changed without it.",
        chips=["ML predicts", "RAG retrieves", "LLM reasons", "agents act", "tests verify"],
    )
    st.markdown("**Part 1 — built today:** commit → analysis → risk → real tests")
    st.graphviz_chart(_themed(BUILT), width="stretch")
    st.markdown("**Part 2 — planned:** what the AI agents will do with a failure")
    st.graphviz_chart(_themed(AGENTS), width="stretch")
    st.caption("Solid = built (phases 1-4) · dashed = planned (phases 5-12) · red = hand-off point to the agents · "
               "hexagon = human approval gate")

    st.markdown("#### Who does what")
    st.markdown(
        "| Component | Responsibility | Boundary | Status |\n|---|---|---|---|\n"
        "| ML risk model | Predicts how risky a change is | Does not find the bug or write the fix | ✅ Built |\n"
        "| CI / test runner | Runs the real tests and gives pass/fail evidence | Never guesses a result | ✅ Built |\n"
        "| Git layer | Branches, commits, diffs | Refuses AI writes to `main` | ✅ Built |\n"
        "| State machine + store | Tracks every step, saves every output | Blocks illegal steps "
        "(e.g. repair without approval) | ✅ Built |\n"
        "| RAG | Retrieves relevant project code, tests and docs | Never claims documents it did not retrieve "
        "| ⏳ Phase 6 |\n"
        "| LLM | Reasons over evidence, writes structured proposals | Outputs are typed and validated | ⏳ Phase 5–7 |\n"
        "| Agents | Investigation, root cause, planning, repair, testing, reflection | Each has one job "
        "| ⏳ Phase 5–11 |\n"
        "| Human | Approves or rejects every proposed fix | Final authority | ⏳ Phase 8 |"
    )

    st.markdown("#### What happens in one run today")
    for i, key in enumerate(["sandbox", "diff", "risk", "tests", "failure", "record"], start=1):
        s = STEPS[key]
        with st.expander(f"{i} · {s['title']}"):
            st.markdown(f"**What happens:** {s['what']}\n\n**Why it matters:** {s['why']}")

    st.markdown("#### What comes next")
    for title, phase, desc in UPCOMING:
        st.markdown(f"- **{title}** _(phase {phase})_ — {desc}")

    st.markdown("#### Safety rules enforced in code")
    st.markdown(
        "| Rule | How it is enforced | Where |\n|---|---|---|\n"
        "| AI never modifies `main` | An AI-actor git repo raises `ProtectedBranchError` on commit/reset/push to "
        "main; AI branches must start with `ai/repair/` | `app/git/repo.py` |\n"
        "| No invented test results | PASS only if pytest exited 0, ≥1 test ran and 0 failed; timeouts and crashes "
        "become TIMEOUT/ERROR | `app/ci/test_runner.py` |\n"
        "| No arbitrary shell commands | Commands are argument lists (no shell); test targets must stay inside the "
        "project | `app/ci/test_runner.py` |\n"
        "| No repair without approval | The state machine only allows APPROVED → REPAIRING | "
        "`app/orchestration/state.py` |\n"
        "| No leaked secrets | Secrets come from env only, are hidden from repr, and are redacted in logs and the "
        "database | `app/config.py`, `app/logging_utils.py` |\n"
        "| No infinite repair loops | Retries capped by `MAX_REPAIR_ATTEMPTS` (default 3) | `app/config.py` |"
    )
