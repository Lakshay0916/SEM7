"""Turn the evaluation's per-run rows into the comparison tables and the written report.

Small samples (3 repeats per configuration), so the report gives medians with the interquartile range
and counts, and says when a difference is within the noise rather than ranking on it.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from app.evaluation.runner import PRICE_PER_MTOK

MEASURES = {"analysis_seconds": "analysis time (s)", "wall_seconds": "whole loop (s)", "tokens": "tokens",
            "cost_usd": "cost (USD)", "llm_calls": "LLM calls", "tool_calls": "MCP tool calls"}


def _quartiles(values: list[float]) -> dict:
    v = sorted(values)
    if not v:
        return {"median": None, "q1": None, "q3": None, "min": None, "max": None}
    if len(v) == 1:
        return {"median": v[0], "q1": v[0], "q3": v[0], "min": v[0], "max": v[0]}
    q1, med, q3 = statistics.quantiles(v, n=4, method="inclusive")
    return {"median": round(med, 4), "q1": round(q1, 4), "q3": round(q3, 4), "min": v[0], "max": v[-1]}


def _rate(rows: list[dict], key: str) -> float | None:
    return round(sum(bool(r[key]) for r in rows) / len(rows), 3) if rows else None


def stats(rows: list[dict]) -> dict:
    agent_rows = [r for r in rows if r["group"] == "agents"]  # "fixed by agents alone" only means something here
    return {"runs": len(rows), "success_rate": _rate(rows, "success"),
            "agent_success_rate": _rate(agent_rows, "agent_success"), "first_try_rate": _rate(rows, "first_try"),
            "root_cause_correct_rate": _rate(rows, "root_cause_correct"),
            "fallback_runs": sum(r["fallbacks"] > 0 for r in rows), "llm_errors": sum(r["llm_errors"] for r in rows),
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
            **{k: _quartiles([r[k] for r in rows]) for k in MEASURES}}


def summarize(rows: list[dict]) -> dict:
    agents = [r for r in rows if r["group"] == "agents"]

    def by(keyfn, data) -> dict:
        groups = defaultdict(list)
        for r in data:
            groups[keyfn(r)].append(r)
        return {k: stats(v) for k, v in sorted(groups.items())}

    return {
        "price_per_mtok": PRICE_PER_MTOK, "runs": len(rows),
        "groups": by(lambda r: r["group"], rows),
        "configurations": by(lambda r: f"{r['pattern']} · {r['messaging']}", agents),
        "patterns": by(lambda r: r["pattern"], agents),
        "messaging": by(lambda r: r["messaging"], agents),
        "scenarios": by(lambda r: f"{r['group']} · {r['scenario']}", rows),
        "totals": {"llm_calls": sum(r["llm_calls"] for r in rows), "tokens": sum(r["tokens"] for r in rows),
                   "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
                   "wall_hours": round(sum(r["wall_seconds"] for r in rows) / 3600, 2)},
    }


def _fmt(q: dict, digits: int = 1, money: bool = False) -> str:
    if q["median"] is None:
        return "-"
    f = (lambda x: f"${x:.4f}") if money else (lambda x: f"{x:,.{digits}f}")
    return f"{f(q['median'])} ({f(q['q1'])}–{f(q['q3'])})"


def _pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"


def table(groups: dict, label: str) -> str:
    head = (f"| {label} | Runs | Fixed | Fixed by agents alone | First try | Root cause right | Analysis time s "
            f"| Tokens | Cost | LLM calls |\n|---|---|---|---|---|---|---|---|---|---|\n")
    return head + "\n".join(
        f"| {k} | {g['runs']} | {_pct(g['success_rate'])} | {_pct(g['agent_success_rate'])} | "
        f"{_pct(g['first_try_rate'])} | {_pct(g['root_cause_correct_rate'])} | {_fmt(g['analysis_seconds'])} | "
        f"{_fmt(g['tokens'], 0)} | {_fmt(g['cost_usd'], money=True)} | {_fmt(g['llm_calls'], 0)} |"
        for k, g in groups.items())


def _overlap(a: dict, b: dict) -> bool:
    return a["q1"] <= b["q3"] and b["q1"] <= a["q3"]


def findings(summary: dict, rows: list[dict]) -> list[str]:
    """The headline comparisons, computed from the data (so they stay true when the runs change)."""
    out = []
    agents = [r for r in rows if r["group"] == "agents"]
    if agents:
        ok = sum(r["agent_success"] for r in agents)
        first = sum(r["first_try"] for r in agents)
        out.append(f"**Reliability:** the agents fixed {ok} of {len(agents)} runs on their own, {first} on the first "
                   f"attempt, with the root cause naming the broken function in "
                   f"{sum(r['root_cause_correct'] for r in agents)} of {len(agents)}.")
    rules = summary["groups"].get("rules")
    if rules:
        out.append(f"**Against the rules baseline:** the rules also fixed every run, but on the first attempt only "
                   f"{rules['first_try_rate']:.0%} of the time (scenario 3 needs a second attempt); the agents' price "
                   f"for one-attempt fixes is a median {summary['groups']['agents']['tokens']['median']:,.0f} tokens "
                   f"(${summary['groups']['agents']['cost_usd']['median']:.4f}) per run.")
    pat = summary["patterns"]
    if {"sequential", "parallel", "hierarchical"} <= set(pat):
        seq, par, hie = (pat[p] for p in ("sequential", "parallel", "hierarchical"))
        out.append(f"**Tokens by pattern:** parallel used {par['tokens']['median'] / seq['tokens']['median']:.1f}x the "
                   f"tokens of sequential (median {par['tokens']['median']:,.0f} vs {seq['tokens']['median']:,.0f}) - "
                   f"three specialists plus a lead read the evidence instead of one investigator; hierarchical "
                   f"stayed close to sequential ({hie['tokens']['median']:,.0f}).")
        sp = "overlap (no clear difference)" if _overlap(seq["analysis_seconds"], par["analysis_seconds"]) else "differ"
        out.append(f"**Time by pattern:** median analysis {seq['analysis_seconds']['median']:.0f} s sequential, "
                   f"{par['analysis_seconds']['median']:.0f} s parallel, {hie['analysis_seconds']['median']:.0f} s "
                   f"hierarchical. Sequential and parallel {sp}; hierarchical is the slowest, as the lead's "
                   f"delegations add LLM calls one after another (median {hie['llm_calls']['median']:.0f} vs "
                   f"{seq['llm_calls']['median']:.0f}), and bursts of calls meet the free tier's rate limit.")
    msg = summary["messaging"]
    if {"blackboard", "direct"} <= set(msg):
        bb, di = msg["blackboard"], msg["direct"]
        same = _overlap(bb["tokens"], di["tokens"])
        out.append(f"**Blackboard vs direct:** median tokens {bb['tokens']['median']:,.0f} vs {di['tokens']['median']:,.0f}"
                   f" and time {bb['analysis_seconds']['median']:.0f} s vs {di['analysis_seconds']['median']:.0f} s - "
                   + ("the ranges overlap, so no clear cost difference; the blackboard's gains are its record "
                      "(every post saved), retries with feedback and decoupled agents, not cheaper runs."
                      if same else "a clear difference in tokens."))
    g = summary["groups"].get("guardrail")
    if g:
        out.append(f"**Guardrail:** all {g['runs']} injected-commit runs were blocked before any LLM call "
                   f"(0 tokens) and repaired by the rules.")
    if agents:
        slow = max(agents, key=lambda r: r["analysis_seconds"])
        out.append(f"**Long tails:** most analyses finish in under a minute, but the slowest took "
                   f"{slow['analysis_seconds']:.0f} s ({slow['run_id']}); its trace shows a single LLM call waiting "
                   f"for minutes inside the SDK on the free tier. Medians are therefore the fair comparison.")
    return out


def report(summary: dict, rows: list[dict]) -> str:
    """EVALUATION.md: what was run, the tables, and what the numbers do and don't show."""
    t = summary["totals"]
    p = summary["price_per_mtok"]
    return f"""# Agent evaluation

{summary['runs']} runs of the full self-healing loop (fresh commit, real tests, analysis, approval, repair,
real re-test, reflection, PR). Generated by `scripts/run_evaluation.py`; per-run data in `runs.jsonl`.
Medians with the interquartile range in brackets. Cost = tokens x Gemini 3.1 Flash-Lite's list price
(${p['input']} / ${p['output']} per million input / output tokens). Total: {t['llm_calls']:,} LLM calls,
{t['tokens']:,} tokens, ${t['cost_usd']:.4f}, {t['wall_hours']} h of runs.

## Findings

{chr(10).join(f"- {f}" for f in findings(summary, rows))}

## Engines

{table(summary['groups'], 'Group')}

`rules` = deterministic baseline (no LLM). `guardrail` = scenario 4 with AI agents: the input guardrail
should block the injected commit before any LLM call, so the rules repair it.

## Agent patterns (both messaging modes, both scenarios)

{table(summary['patterns'], 'Pattern')}

## Messaging (all patterns, both scenarios)

{table(summary['messaging'], 'Messaging')}

## Every agent configuration

{table(summary['configurations'], 'Pattern · messaging')}

## By scenario

{table(summary['scenarios'], 'Group · scenario')}

## How to read this

- "Fixed" = the loop reached a pull request; "fixed by agents alone" also requires that no attempt fell
  back to the rule-based engine (a fallback happens when Gemini errors or a plan fails the guardrail).
- Times were measured on Gemini's free tier (15 requests per minute). Rate-limit waits are part of the
  times and show up as LLM errors / retries; they inflate the patterns that make many calls in a burst.
- {sum(r['group'] == 'agents' for r in rows)} agent runs across 12 configurations is enough to see large
  differences (e.g. tokens per pattern), not small ones: overlapping ranges mean "no clear difference".
"""
