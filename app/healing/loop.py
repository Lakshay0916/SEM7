"""The self-healing loop, shared by every RepairStrategy.

FAILED → INVESTIGATING → ROOT_CAUSE_IDENTIFIED → SOLUTION_PROPOSED → WAITING_APPROVAL
       → (human) APPROVED → REPAIRING → TESTING_REPAIR → PASSED → PR_CREATED
                                                     ↘ REFLECTING → INVESTIGATING … (bounded)
       → (human) REJECTED → INVESTIGATING … or ABORTED

Each call performs one human-sized step, so a UI or CLI can pause for approval.
Every decision and output is recorded in the Store.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.ci.test_runner import run_tests
from app.config import settings
from app.git.repo import GitError
from app.healing.patcher import UnsafePlanError, apply_plan, validate_plan
from app.healing.pr import LocalPRAdapter, PRAdapter
from app.healing.strategy import RepairContext, RepairStrategy
from app.models.schemas import (
    ApprovalDecision,
    FailureEvent,
    RepairPlan,
    Reflection,
    TestReport,
)
from app.models.schemas import WorkflowState as S
from app.orchestration.store import Store
from app.reporting.markdown import render_pr_body

log = logging.getLogger(__name__)
BASE_BRANCH = "main"


class HealingError(RuntimeError):
    pass


def repair_branch(wf_id: str) -> str:
    return f"{settings.repair_branch_prefix}{wf_id}"


def _require(store: Store, wf_id: str, *states: S) -> dict:
    wf = store.get_workflow(wf_id)
    if S(wf["state"]) not in states:
        raise HealingError(f"workflow {wf_id} is {wf['state']}; expected one of "
                           + ", ".join(s.value for s in states))
    return wf


def build_context(store: Store, wf_id: str) -> RepairContext:
    wf = store.get_workflow(wf_id)
    failure = FailureEvent.model_validate(store.latest(wf_id, "failure_event"))
    runs = store.artifacts(wf_id, "test_run")
    current = TestReport.model_validate(runs[-1]["payload"]) if runs else failure.test_report
    applied = {a["attempt"] for a in store.artifacts(wf_id, "repair_attempt")}
    plans = [RepairPlan.model_validate(p["payload"]) for p in store.artifacts(wf_id, "repair_plan")
             if p["attempt"] in applied]
    reflections = [Reflection.model_validate(r["payload"]) for r in store.artifacts(wf_id, "reflection")]
    return RepairContext(
        repo_dir=Path(wf["repo_path"]),
        base_branch=BASE_BRANCH,
        failure=failure,
        current_report=current,
        attempt=wf["attempt"] + 1,
        previous_plans=plans,
        reflections=reflections,
    )


def propose_fix(store: Store, wf_id: str, strategy: RepairStrategy) -> RepairPlan | None:
    """Investigate → root cause → plan, then wait for a human. Changes no code."""
    _require(store, wf_id, S.FAILED, S.REFLECTING, S.REJECTED)
    ctx = build_context(store, wf_id)
    agent = f"{strategy.name}"

    store.transition(wf_id, S.INVESTIGATING, f"Investigating {len(ctx.failing_tests)} failing test(s) "
                     f"(attempt {ctx.attempt})", agent=agent)
    inv = strategy.investigate(ctx)
    store.save_artifact(wf_id, "investigation", inv, attempt=ctx.attempt)

    rc = strategy.root_cause(ctx, inv)
    store.save_artifact(wf_id, "root_cause", rc, attempt=ctx.attempt)
    store.transition(wf_id, S.ROOT_CAUSE_IDENTIFIED, f"{rc.root_cause} (confidence {rc.confidence:.0%})",
                     agent=agent)

    plan = strategy.plan(ctx, inv, rc)
    if plan is None:
        store.transition(wf_id, S.ABORTED, "No applicable repair - handing over to a human", agent=agent)
        return None
    try:
        validate_plan(ctx.repo_dir, plan)
    except UnsafePlanError as exc:
        store.transition(wf_id, S.ABORTED, f"Proposed plan rejected by safety checks: {exc}", agent="safety")
        return None
    store.save_artifact(wf_id, "repair_plan", plan, attempt=plan.attempt)
    store.transition(wf_id, S.SOLUTION_PROPOSED, f"Plan (attempt {plan.attempt}, {plan.level}): {plan.summary}",
                     agent=agent)
    store.transition(wf_id, S.WAITING_APPROVAL, "Waiting for human approval - no code has been changed",
                     agent="orchestrator")
    return plan


def decide(store: Store, wf_id: str, approved: bool, reviewer: str, comment: str = "") -> None:
    _require(store, wf_id, S.WAITING_APPROVAL)
    plan = RepairPlan.model_validate(store.latest(wf_id, "repair_plan"))
    store.save_artifact(wf_id, "approval", ApprovalDecision(approved=approved, reviewer=reviewer,
                                                            comment=comment, plan_attempt=plan.attempt),
                        attempt=plan.attempt)
    store.transition(wf_id, S.APPROVED if approved else S.REJECTED,
                     f"Plan attempt {plan.attempt} {'approved' if approved else 'rejected'} by {reviewer}"
                     + (f": {comment}" if comment else ""), agent="human")


def abort(store: Store, wf_id: str, reason: str, agent: str = "human") -> None:
    store.transition(wf_id, S.ABORTED, reason, agent=agent)


def apply_and_verify(
    store: Store,
    wf_id: str,
    strategy: RepairStrategy | None = None,
    pr_adapter: PRAdapter | None = None,
    max_attempts: int | None = None,
) -> S:
    """Apply the approved plan on the repair branch, run the real tests, then PR or reflect.

    If tests still fail and attempts remain, reflection records what changed and (when a
    strategy is given) a new plan is proposed - which again needs human approval.
    """
    wf = _require(store, wf_id, S.APPROVED)
    plan = RepairPlan.model_validate(store.latest(wf_id, "repair_plan"))
    max_attempts = max_attempts or settings.max_repair_attempts
    branch = repair_branch(wf_id)
    repo_dir = wf["repo_path"]

    store.transition(wf_id, S.REPAIRING, f"Applying plan attempt {plan.attempt} on `{branch}`", agent="repair")
    try:
        attempt = apply_plan(repo_dir, plan, branch, start_point=wf["commit_sha"])
    except (UnsafePlanError, GitError) as exc:
        abort(store, wf_id, f"Repair could not be applied safely: {exc}", agent="repair")
        return S.ABORTED
    store.save_artifact(wf_id, "repair_attempt", attempt, attempt=plan.attempt)
    store.set_attempt(wf_id, plan.attempt)
    store.log_event(wf_id, "repair", "ok", f"Committed {attempt.commit_sha[:10]} on {branch} "
                    f"({', '.join(attempt.changed_files)})")

    store.transition(wf_id, S.TESTING_REPAIR, f"Running the full test suite on `{branch}`", agent="testing")
    report = run_tests(repo_dir)
    store.save_artifact(wf_id, "test_run", report, attempt=plan.attempt)
    store.log_event(wf_id, "testing", report.status.value.lower(),
                    f"{report.status.value}: {report.tests_passed} passed, {report.tests_failed} failed, "
                    f"{report.tests_errored} errors", duration_ms=int(report.duration_seconds * 1000))

    if report.passed:
        store.transition(wf_id, S.PASSED, f"Repair validated: all {report.tests_passed} tests pass", agent="testing")
        adapter = pr_adapter or LocalPRAdapter()
        failure = store.latest(wf_id, "failure_event")
        pr = adapter.open(
            repo_dir=Path(repo_dir), head=branch, base=failure["branch"],
            title=f"[AI repair] Fix {len(failure['failed_tests'])} failing test(s) on {failure['branch']}",
            body=render_pr_body(store.trace(wf_id)),
        )
        store.save_artifact(wf_id, "pull_request", pr, attempt=plan.attempt)
        store.transition(wf_id, S.PR_CREATED, f"PR opened ({pr.provider}): {branch} → {pr.base_branch} "
                         "(not merged)", agent="pr")
        return S.PR_CREATED

    # ---- reflection
    runs = store.artifacts(wf_id, "test_run")
    previous = (TestReport.model_validate(runs[-2]["payload"]) if len(runs) > 1
                else FailureEvent.model_validate(store.latest(wf_id, "failure_event")).test_report)
    before = {f.node_id for f in previous.failures}
    now = {f.node_id for f in report.failures}
    will_retry = plan.attempt < max_attempts
    reflection = Reflection(
        attempt=plan.attempt,
        still_failing=sorted(now & before),
        newly_fixed=sorted(before - now),
        regressions=sorted(now - before),
        summary=(f"Attempt {plan.attempt} ({plan.level}) fixed {len(before - now)} test(s); "
                 f"{len(now & before)} still failing; {len(now - before)} regression(s). "
                 + ("Re-investigating with the new evidence." if will_retry
                    else f"Attempt limit ({max_attempts}) reached.")),
        will_retry=will_retry,
    )
    store.save_artifact(wf_id, "reflection", reflection, attempt=plan.attempt)
    store.transition(wf_id, S.REFLECTING, reflection.summary, agent="reflection")
    if not will_retry:
        abort(store, wf_id, f"Stopped after {max_attempts} repair attempts - human needed", agent="reflection")
        return S.ABORTED
    if strategy is not None:
        propose_fix(store, wf_id, strategy)
    return S(store.get_workflow(wf_id)["state"])
