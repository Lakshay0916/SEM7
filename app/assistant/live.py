"""Read-only views of live data (database + risk model) for the assistant."""

from __future__ import annotations

from collections import Counter

from app.orchestration.store import Store

STATE_MEANING = {
    "RECEIVED": "the commit was received",
    "RISK_ANALYZED": "the commit's risk was scored",
    "TESTING": "the tests are running",
    "FAILED": "the CI tests failed and the failure evidence was packaged",
    "INVESTIGATING": "the failure is being investigated",
    "ROOT_CAUSE_IDENTIFIED": "a root cause was identified",
    "SOLUTION_PROPOSED": "a fix was proposed",
    "WAITING_APPROVAL": "a fix is proposed and waits for your approval - no code has been changed",
    "APPROVED": "the fix was approved",
    "REJECTED": "the proposed fix was rejected - no code was changed",
    "REPAIRING": "the fix is being applied on the repair branch",
    "TESTING_REPAIR": "the tests are re-running on the repair branch",
    "REFLECTING": "the fix was incomplete and the system is analysing why",
    "PASSED": "the tests passed, so no repair was needed",
    "PR_CREATED": "the fix passed the real tests and a pull request was produced (not merged)",
    "ABORTED": "the workflow was stopped",
}

NEXT_STEP = {
    "WAITING_APPROVAL": "Open this workflow on the Run Pipeline or Workflow History page and approve, reject "
                        "or abort the proposed plan.",
    "PR_CREATED": "Review the pull request description. It is never merged automatically.",
    "PASSED": "Nothing to repair. If the gate said REVIEW, a human should still look at the high-risk change.",
    "FAILED": "Run it with self-healing enabled (or call /propose) to get a proposed fix.",
    "REJECTED": "Run the scenario again if you want a new proposal.",
    "ABORTED": "Run the scenario again to start over.",
}


def snapshot(store: Store, predictor=None, recent: int = 5) -> dict:
    wfs = store.list_workflows(limit=500)
    return {
        "total_workflows": len(wfs),
        "by_state": dict(Counter(w["state"] for w in wfs).most_common()),
        "by_scenario": dict(Counter(w["scenario"] or "no scenario (CI gate)" for w in wfs).most_common()),
        "recent": [{k: w[k] for k in ("id", "scenario", "state", "attempt", "created_at")} for w in wfs[:recent]],
        "risk_model": getattr(predictor, "version", None),
        "database": store.backend,
    }


def render_snapshot(snap: dict) -> str:
    lines = [f"Database: {snap['database']}. Workflows recorded: {snap['total_workflows']}."]
    if snap["by_state"]:
        lines.append("By final state: " + ", ".join(f"{s} {n}" for s, n in snap["by_state"].items()) + ".")
    if snap["by_scenario"]:
        lines.append("By scenario: " + ", ".join(f"{s} {n}" for s, n in snap["by_scenario"].items()) + ".")
    lines.append(f"Risk model version: {snap['risk_model'] or 'not trained'}.")
    if snap["recent"]:
        lines.append("\nMost recent runs:")
        lines += [f"- {w['id']} | {w['scenario'] or '-'} | {w['state']} | attempt {w['attempt']} | "
                  f"{w['created_at'][:19].replace('T', ' ')}" for w in snap["recent"]]
    return "\n".join(lines)


def workflow_digest(store: Store, wf_id: str) -> dict:
    """The facts a person asks about one run. Raises KeyError for an unknown id."""
    t = store.trace(wf_id)
    wf = t["workflow"]

    def latest(kind: str) -> dict | None:
        items = [a["payload"] for a in t["artifacts"] if a["kind"] == kind]
        return items[-1] if items else None

    risk, gate, cause = latest("risk_assessment"), latest("quality_gate"), latest("root_cause")
    plan, test, refl, pr = latest("repair_plan"), latest("test_run"), latest("reflection"), latest("pull_request")
    run = latest("pipeline_run") or {}
    return {
        "id": wf["id"], "scenario": wf["scenario"], "state": wf["state"], "attempt": wf["attempt"],
        "branch": wf["branch"], "commit_message": (run.get("commit") or {}).get("message"),
        "meaning": STATE_MEANING.get(wf["state"], ""), "next_step": NEXT_STEP.get(wf["state"]),
        "risk": risk and {"level": risk["risk_level"], "score": risk["risk_score"],
                          "factors": risk.get("risk_factors", [])},
        "gate": gate and {"status": gate["status"], "reasons": gate.get("reasons", [])},
        "root_cause": cause and {"text": cause["root_cause"], "confidence": cause.get("confidence")},
        "plan": plan and plan["summary"],
        "last_test": test and {k: test[k] for k in ("status", "tests_run", "tests_passed", "tests_failed")},
        "reflection": refl and refl["summary"],
        "pull_request": pr and {"title": pr["title"], "head": pr["head_branch"], "base": pr["base_branch"],
                                "merged": pr["merged"]},
        "timeline": [f"{e['state'] or '·'} [{e['agent']}/{e['status']}] {e['message']}" for e in t["events"]],
    }


def render_digest(d: dict) -> str:
    out = [f"Workflow {d['id']} (scenario {d['scenario']}, branch {d['branch']}, "
           f"commit \"{d['commit_message']}\"): state {d['state']} - {d['meaning']}. Repair attempts: {d['attempt']}."]
    if d["risk"]:
        f = "; ".join(d["risk"]["factors"]) or "no specific factors"
        out.append(f"Risk: {d['risk']['level']} (score {d['risk']['score']:.3f}) - {f}.")
    if d["gate"]:
        out.append(f"Quality gate: {d['gate']['status']} - {'; '.join(d['gate']['reasons'])}.")
    if d["root_cause"]:
        conf = d["root_cause"]["confidence"]
        out.append(f"Root cause: {d['root_cause']['text']}" + (f" (confidence {conf:.0%})." if conf else "."))
    if d["plan"]:
        out.append(f"Latest plan: {d['plan']}.")
    if d["last_test"]:
        lt = d["last_test"]
        out.append(f"Latest test run: {lt['status']} - {lt['tests_passed']}/{lt['tests_run']} passed.")
    if d["reflection"]:
        out.append(f"Reflection: {d['reflection']}")
    if d["pull_request"]:
        pr = d["pull_request"]
        out.append(f"Pull request: \"{pr['title']}\" ({pr['head']} -> {pr['base']}, "
                   f"{'merged' if pr['merged'] else 'not merged'}).")
    if d["next_step"]:
        out.append(f"Next step: {d['next_step']}")
    out.append("Timeline:\n" + "\n".join(f"- {line}" for line in d["timeline"]))
    return "\n".join(out)
