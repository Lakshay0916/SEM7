"""Tool logic behind the two MCP servers (plain Python, so it is testable without MCP).

Every tool is scoped to ONE repair context given by the server's environment, never by the model:
    SEM7_REPO_DIR   sandbox repository being repaired
    SEM7_BASE_SHA   healthy baseline commit          SEM7_HEAD_SHA   developer's commit
    DATABASE_URL    workflow store (for past-failure lookups)
so an agent cannot point a tool at an arbitrary path or database.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT
from app.git.diff import extract_diff, is_config_file, is_dependency_file, is_test_file
from app.git.repo import GitRepo
from app.healing.code_utils import function_source

MAX_CHARS = 6000


@dataclass(frozen=True)
class RepairScope:
    repo_dir: Path
    base_sha: str
    head_sha: str

    @classmethod
    def from_env(cls) -> RepairScope:
        return cls(Path(os.environ["SEM7_REPO_DIR"]), os.environ["SEM7_BASE_SHA"], os.environ["SEM7_HEAD_SHA"])

    def env(self) -> dict[str, str]:
        return {"SEM7_REPO_DIR": str(self.repo_dir), "SEM7_BASE_SHA": self.base_sha, "SEM7_HEAD_SHA": self.head_sha}

    def resolve(self, path: str) -> Path:
        target = (self.repo_dir / path).resolve()
        if not target.is_relative_to(self.repo_dir.resolve()) or ".git" in Path(path).parts:
            raise ValueError(f"{path}: outside the repository")
        return target


# --------------------------------------------------------------------------- sem7-risk (ML)


def score_change_risk(scope: RepairScope) -> dict:
    """ML risk score of the developer's commit (the project's trained risk model)."""
    from app.risk.predictor import MODEL_PATH, RiskPredictor

    if not MODEL_PATH.exists():
        return {"error": "risk model not trained"}
    repo = GitRepo(scope.repo_dir)
    diff = extract_diff(repo, scope.base_sha, scope.head_sha)
    message = repo.commit_info(scope.head_sha).message
    risk = RiskPredictor.load().assess(diff, message, commit_sha=scope.head_sha, is_pull_request=True)
    return {"risk_level": risk.risk_level.value, "risk_score": risk.risk_score,
            "risk_factors": risk.risk_factors, "model_version": risk.model_version,
            "files_changed": [f.path for f in diff.files], "lines_added": diff.total_additions,
            "lines_deleted": diff.total_deletions}


def risk_model_card() -> dict:
    """What the risk model is and how well it performs on held-out data (from the real evaluation)."""
    path = PROJECT_ROOT / "data" / "risk" / "evaluation.json"
    if not path.exists():
        return {"error": "no evaluation available"}
    ev = json.loads(path.read_text(encoding="utf-8"))
    out = {"version": ev.get("version"), "dataset": ev.get("dataset"), "variants": {}}
    for name, v in ev["variants"].items():
        test = v["candidates"][v["chosen_model"]]["test"]
        out["variants"][name] = {"model": v["chosen_model"], "test_roc_auc": test.get("roc_auc"),
                                 "test_pr_auc": test.get("pr_auc"),
                                 "failure_rate_by_level": v.get("test_failure_rate_by_risk_level")}
    return out


# --------------------------------------------------------------------------- sem7-ci (code, tests, database)


def read_source(scope: RepairScope, path: str, function: str = "", version: str = "current") -> dict:
    """Source of a file or one function: version = current (working tree), baseline or commit."""
    target = scope.resolve(path)
    if version == "current":
        if not target.is_file():
            return {"error": f"{path}: no such file"}
        text = target.read_text(encoding="utf-8")
    elif version in ("baseline", "commit"):
        sha = scope.base_sha if version == "baseline" else scope.head_sha
        try:
            text = GitRepo(scope.repo_dir).show_file(sha, path)
        except Exception:
            return {"error": f"{path}: not present at {version} ({sha[:10]})"}
    else:
        return {"error": "version must be current, baseline or commit"}
    if function:
        src = function_source(text, function)
        if src is None:
            return {"error": f"function {function!r} not found in {path} ({version})"}
        text = src
    return {"path": path, "function": function or None, "version": version,
            "source": text[:MAX_CHARS], "truncated": len(text) > MAX_CHARS}


def run_tests_with_patch(scope: RepairScope, path: str, function: str, new_source: str) -> dict:
    """Dry run: replace one function in a throwaway copy of the repo and run the real test suite.

    The repository itself is never modified - this only tells the agent whether a candidate fix works.
    """
    from app.agents.guardrails import _reindent
    from app.ci.test_runner import run_tests

    if is_test_file(path) or is_config_file(path) or is_dependency_file(path):
        return {"error": f"{path}: fixes may only change source code, not tests/config/dependencies"}
    target = scope.resolve(path)
    if not target.is_file():
        return {"error": f"{path}: no such file"}
    current = function_source(target.read_text(encoding="utf-8"), function)
    if current is None:
        return {"error": f"function {function!r} not found in {path}"}
    with tempfile.TemporaryDirectory(prefix="sem7-dryrun-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(scope.repo_dir, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        f = copy / path
        f.write_text(f.read_text(encoding="utf-8").replace(current, _reindent(new_source, current), 1),
                     encoding="utf-8")
        report = run_tests(copy)
    return {"status": report.status.value, "tests_run": report.tests_run, "passed": report.tests_passed,
            "failed": report.tests_failed, "errors": report.tests_errored,
            "failures": [{"test": x.node_id, "message": (x.message or "").splitlines()[0][:200] if x.message else ""}
                         for x in report.failures][:10]}


def past_failures(query: str, limit: int = 5) -> dict:
    """Database lookup: earlier workflows whose root cause or fix mentions the query terms."""
    from app.orchestration.store import Store

    store = Store()
    terms = [t for t in query.lower().replace("(", " ").replace(")", " ").split() if len(t) > 2]
    if not terms:
        return {"matches": []}
    matches = []
    for wf in store.list_workflows(limit=300):
        rc, plan = store.latest(wf["id"], "root_cause"), store.latest(wf["id"], "repair_plan")
        if not rc:
            continue
        text = f"{rc.get('root_cause', '')} {(plan or {}).get('summary', '')}".lower()
        score = sum(t in text for t in terms)
        if score:
            matches.append((score, {"workflow_id": wf["id"], "scenario": wf["scenario"], "final_state": wf["state"],
                                    "root_cause": rc.get("root_cause"), "fix": (plan or {}).get("summary"),
                                    "fixed_by": (plan or {}).get("strategy")}))
    matches.sort(key=lambda m: m[0], reverse=True)
    return {"query": query, "matches": [m for _, m in matches[:limit]]}
