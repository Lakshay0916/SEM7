"""Orchestrates the implemented part of the workflow and records it in the Store:

RECEIVED -> RISK_ANALYZED -> TESTING -> PASSED | FAILED

Later phases (investigation, RAG, root cause, approval, repair, ...) continue
from FAILED.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.ci.scenarios import get_scenario
from app.ci.simulator import StepCallback, create_sandbox, run_pipeline
from app.models.schemas import PipelineRun
from app.models.schemas import WorkflowState as S
from app.orchestration.store import Store

log = logging.getLogger(__name__)


@dataclass
class WorkflowResult:
    workflow_id: str
    repo_dir: Path
    run: PipelineRun


def run_ci_workflow(
    scenario_id: str,
    store: Store,
    predictor=None,
    workspace: Path | None = None,
    on_step: StepCallback | None = None,
) -> WorkflowResult:
    scenario = get_scenario(scenario_id)
    if on_step:
        on_step("sandbox", "Creating sandbox git repo and developer commit")
    repo_dir = create_sandbox(scenario, workspace)
    if on_step:
        on_step("sandbox", f"Commit on {scenario.branch}: {scenario.commit_message}")
    run = run_pipeline(repo_dir, predictor=predictor, on_step=on_step)

    wf = store.create_workflow(repo_dir, scenario.id, run.commit.sha, run.commit.branch)

    if run.risk is not None:
        store.save_artifact(wf, "risk_assessment", run.risk)
        store.transition(wf, S.RISK_ANALYZED, f"Risk = {run.risk.risk_level.value} "
                         f"(score {run.risk.risk_score:.3f})", agent="risk_model")
    else:
        store.log_event(wf, "risk_model", "skipped", "No trained risk model available")

    report = run.test_report
    store.transition(wf, S.TESTING, f"Tests started: {report.command}", agent="ci")
    store.save_artifact(wf, "pipeline_run", run)
    store.log_event(
        wf, "ci", report.status.value.lower(),
        f"{report.status.value}: {report.tests_passed} passed, {report.tests_failed} failed, "
        f"{report.tests_errored} errors",
        duration_ms=int(report.duration_seconds * 1000),
    )
    if run.failure_event is None:
        store.transition(wf, S.PASSED, "All tests passed - no repair needed", agent="ci")
    else:
        store.save_artifact(wf, "failure_event", run.failure_event)
        store.transition(wf, S.FAILED, f"CI failure detected: {len(run.failure_event.failed_tests)} "
                         "failing test(s)", agent="ci")
    log.info("workflow %s finished CI stage in state %s", wf, store.state(wf).value)
    return WorkflowResult(workflow_id=wf, repo_dir=repo_dir, run=run)
