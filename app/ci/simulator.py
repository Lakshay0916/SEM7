"""Local CI simulator: sandbox repo -> developer commit -> diff -> real tests -> FailureEvent.

This lets the full pipeline be demonstrated without depending on cloud CI.
The same `run_pipeline` is used for an existing repository checkout.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Callable

from app.ci.scenarios import Scenario
from app.ci.test_runner import run_tests
from app.config import settings
from app.git.diff import extract_diff
from app.git.repo import GitRepo
from app.models.schemas import FailureEvent, PipelineRun, RiskAssessment, TestStatus

BASE_BRANCH = "main"

StepCallback = Callable[[str, str], None]  # (step id, human-readable detail)


def _noop(step: str, detail: str) -> None:
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def create_sandbox(scenario: Scenario, workspace: Path | None = None) -> Path:
    """Copy the demo project into a fresh git repo and apply the scenario's developer commit.

    Result: `main` holds the healthy baseline; `scenario.branch` holds the change under test
    and is checked out.
    """
    workspace = workspace or settings.workspace_dir
    repo_dir = workspace / "runs" / new_id(scenario.id) / "repo"
    shutil.copytree(
        scenario.project_dir, repo_dir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )

    repo = GitRepo.init(repo_dir, actor="developer", branch=BASE_BRANCH)
    repo.commit_all("Initial commit: healthy baseline")

    repo.create_branch(scenario.branch)
    for edit in scenario.edits:
        path = repo_dir / edit.file
        if edit.old == "":
            if path.exists():
                raise ValueError(f"scenario would overwrite existing file {edit.file}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(edit.new)
            continue
        content = path.read_text()
        if edit.old not in content:
            raise ValueError(f"scenario edit target not found in {edit.file}: {edit.old!r}")
        path.write_text(content.replace(edit.old, edit.new, 1))
    repo.commit_all(scenario.commit_message)
    return repo_dir


def run_pipeline(
    repo_dir: str | Path,
    base_branch: str = BASE_BRANCH,
    predictor=None,
    on_step: StepCallback | None = None,
) -> PipelineRun:
    """Run 'CI' on the currently checked-out commit of repo_dir.

    If a RiskPredictor is given, the commit is risk-scored before tests run.
    `on_step` is called as each stage starts/finishes (used for live UI progress).
    """
    on_step = on_step or _noop
    repo = GitRepo(repo_dir)
    commit = repo.commit_info()
    base = repo.merge_base(base_branch, "HEAD")

    on_step("diff", f"Comparing {commit.branch} with {base_branch}")
    diff = extract_diff(repo, base, "HEAD")
    on_step("diff", f"{len(diff.files)} file(s) changed, +{diff.total_additions}/-{diff.total_deletions}")

    risk: RiskAssessment | None = None
    if predictor is not None:
        on_step("risk", "Scoring the change with the ML risk model")
        # A commit on a non-base branch is tested in pull-request context, matching
        # how `is_pull_request` is defined in the mined training data.
        risk = predictor.assess(
            diff, commit.message, commit_sha=commit.sha,
            is_pull_request=commit.branch != base_branch,
        )
        on_step("risk", f"Risk {risk.risk_level.value} (score {risk.risk_score:.3f})")
    else:
        on_step("risk", "Skipped - no trained risk model")

    on_step("tests", "Running the real pytest suite in the sandbox")
    report = run_tests(repo_dir)
    on_step("tests", f"{report.status.value}: {report.tests_passed} passed, {report.tests_failed} failed")
    pipeline_id = new_id("pipeline")

    failure = None
    if report.status != TestStatus.PASS:
        failure = FailureEvent(
            commit_sha=commit.sha,
            branch=commit.branch,
            pipeline_id=pipeline_id,
            status=report.status.value.lower(),
            failed_tests=[f.node_id for f in report.failures],
            logs=(report.stdout + ("\n" + report.stderr if report.stderr else "")).strip(),
            changed_files=diff.changed_files,
            test_report=report,
            diff=diff,
        )
    return PipelineRun(
        pipeline_id=pipeline_id, commit=commit, diff=diff, risk=risk, test_report=report,
        failure_event=failure,
    )
