"""Markdown reports built only from recorded artifacts (nothing is inferred or invented)."""

from __future__ import annotations

from app.models.schemas import (
    CommitInfo,
    DiffSummary,
    GateDecision,
    RiskAssessment,
    TestReport,
)

GATE_ICON = {"PASS": "✅", "REVIEW": "🟠", "BLOCK": "❌"}


def _by_kind(trace: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in trace["artifacts"]:
        out.setdefault(a["kind"], []).append(a)
    return out


def _tests_line(r: dict) -> str:
    return (f"**{r['status']}** — {r['tests_passed']} passed, {r['tests_failed']} failed, "
            f"{r['tests_errored']} errors (`{r['command']}`, exit {r['exit_code']}, {r['duration_seconds']}s)")


def render_pr_body(trace: dict) -> str:
    k = _by_kind(trace)
    wf = trace["workflow"]
    failure = k["failure_event"][-1]["payload"]
    plan = k["repair_plan"][-1]["payload"]
    rc = k["root_cause"][-1]["payload"]
    tests = k["test_run"][-1]["payload"]
    attempts = k.get("repair_attempt", [])
    risk = k.get("risk_assessment", [{}])[-1].get("payload")
    approvals = [a["payload"] for a in k.get("approval", [])]

    lines = [
        "## Problem",
        f"Commit `{failure['commit_sha'][:10]}` on `{failure['branch']}` failed CI: "
        + ", ".join(f"`{t}`" for t in failure["failed_tests"]),
        "",
        "## Root cause",
        f"{rc['root_cause']} (confidence {rc['confidence']:.0%}, {plan['strategy_kind']} analysis)",
        *[f"- {e}" for e in rc["evidence"][:6]],
        "",
        "## Risk",
        f"{risk['risk_level']} (score {risk['risk_score']:.3f}, model `{risk['model_version']}`)" if risk
        else "Not assessed",
        "",
        "## Fix",
        f"{plan['summary']} — strategy `{plan['strategy']}`, level `{plan['level']}`",
        *[f"- `{c['file']}`: {c['description']}" for c in plan["changes"]],
        "",
        "## Repair attempts",
        *[f"- Attempt {a['attempt']}: commit `{a['payload']['commit_sha'][:10]}` changed "
          + ", ".join(f"`{f}`" for f in a["payload"]["changed_files"]) for a in attempts],
        "",
        "## Tests executed",
        _tests_line(tests),
        "",
        "## Human approvals",
        *[f"- Attempt {a['plan_attempt']}: {'approved' if a['approved'] else 'rejected'} by {a['reviewer']}"
          + (f" — {a['comment']}" if a["comment"] else "") for a in approvals],
        "",
        f"_Workflow `{wf['id']}` · generated from recorded artifacts · this PR is never merged automatically._",
    ]
    return "\n".join(lines)


def render_workflow_report(trace: dict) -> str:
    k = _by_kind(trace)
    wf = trace["workflow"]
    run = k["pipeline_run"][-1]["payload"] if "pipeline_run" in k else None
    lines = [f"# Workflow report `{wf['id']}`", "",
             f"- Scenario: `{wf['scenario']}`", f"- Final state: **{wf['state']}**",
             f"- Branch / commit: `{wf['branch']}` / `{(wf['commit_sha'] or '')[:10]}`",
             f"- Repair attempts: {wf['attempt']}", ""]
    if run:
        lines += ["## CI run", f"- Changed files: " + ", ".join(f"`{f['path']}`" for f in run["diff"]["files"]),
                  f"- Tests: {_tests_line(run['test_report'])}"]
        if run.get("risk"):
            r = run["risk"]
            lines.append(f"- Risk: **{r['risk_level']}** (score {r['risk_score']:.3f})")
            lines += [f"  - {f}" for f in r["risk_factors"]]
        lines.append("")
    for i, p in enumerate(k.get("repair_plan", []), 1):
        plan = p["payload"]
        lines += [f"## Attempt {plan['attempt']}: {plan['summary']}",
                  f"- Strategy `{plan['strategy']}` ({plan['strategy_kind']}), level `{plan['level']}`",
                  f"- Root cause: {plan['root_cause']}"]
        lines += [f"- Change `{c['file']}`: {c['description']}" for c in plan["changes"]]
        for a in k.get("approval", []):
            if a["payload"]["plan_attempt"] == plan["attempt"]:
                ap = a["payload"]
                lines.append(f"- Decision: {'APPROVED' if ap['approved'] else 'REJECTED'} by {ap['reviewer']}")
        for t in k.get("test_run", []):
            if t["attempt"] == plan["attempt"]:
                lines.append(f"- Re-test: {_tests_line(t['payload'])}")
        for r in k.get("reflection", []):
            if r["attempt"] == plan["attempt"]:
                lines.append(f"- Reflection: {r['payload']['summary']}")
        lines.append("")
    for pr in k.get("pull_request", []):
        p = pr["payload"]
        lines += ["## Pull request", f"- {p['title']} (`{p['head_branch']}` → `{p['base_branch']}`, "
                  f"{p['provider']}, not merged)", f"- {p['url']}", ""]
    lines += ["## Timeline", "", "| time (UTC) | state | agent | message |", "|---|---|---|---|"]
    lines += [f"| {e['ts'][11:19]} | {e['state'] or ''} | {e['agent']} | {e['message']} |" for e in trace["events"]]
    return "\n".join(lines) + "\n"


def render_gate_summary(commit: CommitInfo, diff: DiffSummary, risk: RiskAssessment | None,
                        report: TestReport, gate: GateDecision) -> str:
    lines = [
        f"## {GATE_ICON[gate.status.value]} Quality gate: **{gate.status.value}**", "",
        f"Commit `{commit.sha[:10]}` on `{commit.branch}` — _{commit.message.splitlines()[0]}_", "",
        "| Check | Result |", "|---|---|",
        f"| Tests | {report.status.value}: {report.tests_passed} passed, {report.tests_failed} failed, "
        f"{report.tests_errored} errors ({report.duration_seconds}s) |",
        f"| Risk | {risk.risk_level.value} (score {risk.risk_score:.3f}, `{risk.model_version}`) |" if risk
        else "| Risk | not assessed |",
        f"| Change | {len(diff.files)} file(s), +{diff.total_additions}/-{diff.total_deletions} |", "",
        "**Why:**", *[f"- {r}" for r in gate.reasons],
    ]
    if report.failures:
        lines += ["", "**Failing tests:**", *[f"- `{f.node_id}` — {f.message.splitlines()[0] if f.message else ''}"
                                             for f in report.failures[:20]]]
    if risk and risk.risk_factors:
        lines += ["", "**Risk factors:**", *[f"- {f}" for f in risk.risk_factors]]
    lines += ["", "_Policy: tests always run; HIGH risk only adds a review requirement and never skips tests._"]
    return "\n".join(lines) + "\n"
