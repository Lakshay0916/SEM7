"""The evaluation: run the full self-healing loop many times and measure every run from its records.

One run = a fresh sandbox commit -> real tests -> analysis (rules or agents) -> approval -> repair ->
real re-test -> reflection and another attempt if needed -> PR or abort. Approval is given automatically
here (and recorded as reviewer "evaluation") - the only way to time the whole loop without a person;
in the product every plan still waits for a human.

Every number comes from what the run recorded: workflow states and artifacts, the event log, and the
saved traces (LLM calls and tokens from the ADK / CrewAI callbacks). Nothing is estimated except cost,
which is tokens x the published price (PRICE_PER_MTOK).
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass

from app.healing.loop import apply_and_verify, decide, propose_fix
from app.models.schemas import WorkflowState as S
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.store import Store

# Gemini 3.1 Flash-Lite list price, standard tier, USD per million tokens (ai.google.dev, October 2026)
PRICE_PER_MTOK = {"input": 0.25, "output": 1.50}
# The function each scenario's commit broke, to check the root cause names it
EXPECTED_FUNCTION = {"simple_bug": "add", "failed_first_repair": "divide", "injected_commit": "add"}
DAILY_QUOTA = re.compile(r"per ?day|PerDay|requests_per_day", re.I)


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    group: str            # agents | rules | guardrail
    scenario: str
    pattern: str | None = None
    messaging: str | None = None
    repeat: int = 1


def matrix(repeats: int = 3) -> list[RunSpec]:
    """~40 runs: 3 patterns x 2 messaging modes x 2 scenarios x `repeats`, a rule-based baseline, and the
    injected-commit scenario. Repeat-major order spreads each configuration over the whole session."""
    runs = [RunSpec(f"rules-{sc}-{r}", "rules", sc, repeat=r)
            for r in (1, 2) for sc in ("simple_bug", "failed_first_repair")]
    runs += [RunSpec(f"guardrail-injected_commit-{r}", "guardrail", "injected_commit", "sequential", "blackboard", r)
             for r in (1, 2)]
    runs += [RunSpec(f"agents-{sc}-{p}-{m}-{r}", "agents", sc, p, m, r)
             for r in range(1, repeats + 1) for sc in ("simple_bug", "failed_first_repair")
             for p in ("sequential", "parallel", "hierarchical") for m in ("blackboard", "direct")]
    return runs


def make_strategy(spec: RunSpec):
    if spec.group == "rules":
        from app.healing.rules import RuleBasedStrategy

        return RuleBasedStrategy()
    from app.agents.strategy import AgenticStrategy

    return AgenticStrategy(messaging=spec.messaging, pattern=spec.pattern, transport="local", fallback=True)


def run_one(store: Store, spec: RunSpec, max_rounds: int = 6) -> dict:
    """Run the whole loop once (auto-approving each plan) and return its measurements."""
    strategy = make_strategy(spec)
    wf = run_ci_workflow(spec.scenario, store, None, strategy=None).workflow_id
    start = time.monotonic()
    propose_fix(store, wf, strategy)
    first_analysis = time.monotonic() - start
    for _ in range(max_rounds):
        if S(store.get_workflow(wf)["state"]) != S.WAITING_APPROVAL:
            break
        decide(store, wf, True, "evaluation", "auto-approved by the evaluation runner")
        apply_and_verify(store, wf, strategy=strategy)  # repair + real tests (+ re-analysis if it failed)
    out = {**asdict(spec), **measure(store, wf, spec), "wall_seconds": round(time.monotonic() - start, 2)}
    if not out["analysis_seconds"]:  # rule-based runs have no trace: time their (first) analysis directly
        out["analysis_seconds"] = round(first_analysis, 3)
    return out


def measure(store: Store, wf: str, spec: RunSpec) -> dict:
    """Everything the evaluation reports for one workflow, read back from its records."""
    state = store.get_workflow(wf)["state"]
    plans = [a["payload"] for a in store.artifacts(wf, "repair_plan")]
    events = store.events(wf)
    spans = [s for a in store.artifacts(wf, "trace") for s in a["payload"]["spans"]]
    llm = [s for s in spans if s["kind"] == "llm"]
    prompt = sum(s["attributes"].get("prompt_tokens") or 0 for s in llm)
    output = sum(s["attributes"].get("output_tokens") or 0 for s in llm)
    rc = store.artifacts(wf, "root_cause")
    first_rc = rc[0]["payload"]["root_cause"] if rc else ""
    expected = EXPECTED_FUNCTION.get(spec.scenario)
    errors_text = " ".join(e["message"] or "" for e in events if e["status"] in ("fallback", "error", "retry"))
    agent_plans = sum(p["strategy"] == "adk+crewai" for p in plans)
    return {
        "workflow_id": wf, "final_state": state, "success": state == S.PR_CREATED.value,
        "attempts": len(plans), "first_try": state == S.PR_CREATED.value and len(plans) == 1,
        "plans_by_agents": agent_plans, "plans_by_rules": len(plans) - agent_plans,
        "agent_success": state == S.PR_CREATED.value and len(plans) > 0 and agent_plans == len(plans),
        "fallbacks": sum(e["status"] == "fallback" for e in events),
        "guardrail_blocked": any(e["agent"] == "system:input guardrail" and e["status"] == "blocked" for e in events),
        "root_cause_correct": bool(expected and re.search(rf"\b{expected}\b", first_rc, re.I)),
        "analysis_seconds": round(sum(s["seconds"] for s in spans if s["kind"] == "attempt"), 2),
        "llm_calls": len(llm), "llm_errors": sum(s["status"] == "error" for s in llm),
        "prompt_tokens": prompt, "output_tokens": output, "tokens": prompt + output,
        "cost_usd": round(prompt / 1e6 * PRICE_PER_MTOK["input"] + output / 1e6 * PRICE_PER_MTOK["output"], 6),
        "tool_calls": sum(s["kind"] == "tool" for s in spans),
        "agents_run": sum(s["kind"] == "agent" for s in spans),
        "retries": sum(e["status"] == "retry" for e in events),
        "daily_quota_hit": bool(DAILY_QUOTA.search(errors_text)),
    }
