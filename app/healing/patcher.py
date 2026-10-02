"""Apply an APPROVED repair plan - safely.

Safety rules enforced here, regardless of which strategy produced the plan:
* changes go only to an `ai/repair/<id>` branch (GitRepo(actor="ai") refuses main)
* files must be inside the repository
* tests, CI config and dependency manifests may not be modified (no "fixing"
  a failure by editing the test that detects it)
* each `original` snippet must occur exactly once (no ambiguous edits)
* only the files named in the plan are committed
"""

from __future__ import annotations

from pathlib import Path

from app.git.diff import is_config_file, is_dependency_file, is_test_file
from app.git.repo import GitRepo
from app.models.schemas import RepairAttempt, RepairPlan


class UnsafePlanError(ValueError):
    pass


def validate_plan(repo_dir: Path, plan: RepairPlan) -> None:
    if not plan.changes:
        raise UnsafePlanError("plan contains no changes")
    root = repo_dir.resolve()
    for ch in plan.changes:
        target = (root / ch.file).resolve()
        if not target.is_relative_to(root) or ".git" in Path(ch.file).parts:
            raise UnsafePlanError(f"{ch.file}: outside the repository")
        if is_test_file(ch.file):
            raise UnsafePlanError(f"{ch.file}: repairs may not modify tests")
        if is_config_file(ch.file) or is_dependency_file(ch.file):
            raise UnsafePlanError(f"{ch.file}: repairs may not modify CI/config/dependency files")
        if not target.is_file():
            raise UnsafePlanError(f"{ch.file}: file does not exist")
        count = target.read_text(encoding="utf-8").count(ch.original)
        if count != 1:
            raise UnsafePlanError(f"{ch.file}: snippet to replace found {count} times (must be exactly 1)")


def apply_plan(repo_dir: str | Path, plan: RepairPlan, branch: str, start_point: str) -> RepairAttempt:
    """Create/checkout `branch` (from `start_point` the first time), apply, commit. Returns the attempt."""
    repo_dir = Path(repo_dir)
    ai = GitRepo(repo_dir, actor="ai")
    if ai.branch_exists(branch):
        ai.checkout(branch)
    else:
        ai.create_branch(branch, start_point=start_point)
    validate_plan(repo_dir, plan)  # validate against the branch the change will land on

    before = ai.head_sha()
    files = []
    for ch in plan.changes:
        path = repo_dir / ch.file
        text = path.read_text(encoding="utf-8").replace(ch.original, ch.replacement, 1)
        path.write_text(text, encoding="utf-8")
        files.append(ch.file)
    sha = ai.commit_paths(sorted(set(files)),
                          f"AI repair attempt {plan.attempt} ({plan.strategy}/{plan.level}): {plan.summary}")
    return RepairAttempt(
        attempt=plan.attempt,
        branch=branch,
        base_commit=before,
        commit_sha=sha,
        changed_files=sorted(set(files)),
        patch=ai.diff_text(before, sha),
    )
