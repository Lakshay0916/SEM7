"""Commit -> numeric feature vector for risk prediction.

Shared by dataset mining (public repos) and live prediction (pipeline commits),
so training and inference compute features identically.

Two feature groups:
* diff features    - computable from the commit alone
* history features - need prior CI outcomes of the same repo (only ever
                     computed from results known *before* the commit was tested)
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from app.models.schemas import DiffSummary

DB_RE = re.compile(r"(migrat|sql|database|/db/|_db\b|\bdb_|models?\.py$)", re.I)
SECURITY_RE = re.compile(
    r"(auth|login|passw|token|secret|crypt|secur|permission|session|oauth|jwt|ssl|cert)", re.I
)
FIX_RE = re.compile(r"\b(fix|fixes|fixed|bug|patch|hotfix|regression|revert)\b", re.I)
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}

DIFF_FEATURES = [
    "n_files",
    "lines_added",
    "lines_deleted",
    "log_churn",
    "n_functions_changed",
    "n_modules",
    "n_src_files",
    "n_test_files",
    "n_docs_files",
    "n_dependency_files",
    "n_config_files",
    "ci_config_changed",
    "n_db_related",
    "n_security_related",
    "src_without_tests",
    "docs_only",
    "is_pull_request",
    "is_merge",
    "msg_mentions_fix",
]

HISTORY_FEATURES = [
    "hist_file_max_failure_rate",
    "hist_file_mean_failure_rate",
    "hist_file_mean_prior_changes",
    "author_prior_commits",
    "author_failure_rate",
    "parent_known",
    "parent_failed",
    "repo_recent_failure_rate",
]

ALL_FEATURES = DIFF_FEATURES + HISTORY_FEATURES


@dataclass
class HistoryContext:
    """Prior CI knowledge for a commit. All zeros = no history available."""

    hist_file_max_failure_rate: float = 0.0
    hist_file_mean_failure_rate: float = 0.0
    hist_file_mean_prior_changes: float = 0.0
    author_prior_commits: float = 0.0
    author_failure_rate: float = 0.0
    parent_known: float = 0.0
    parent_failed: float = 0.0
    repo_recent_failure_rate: float = 0.0


def _is_doc(path: str) -> bool:
    p = PurePosixPath(path)
    if p.name.lower().startswith("requirements"):
        return False
    return p.suffix.lower() in DOC_SUFFIXES or "docs" in p.parts or "doc" in p.parts


def diff_features(
    diff: DiffSummary, message: str = "", is_pull_request: bool = False, is_merge: bool = False
) -> dict[str, float]:
    files = diff.files
    docs = [f for f in files if _is_doc(f.path)]
    tests = [f for f in files if f.is_test]
    src = [
        f for f in files
        if not (f.is_test or f.is_config or f.is_dependency or _is_doc(f.path))
    ]
    added, deleted = diff.total_additions, diff.total_deletions
    return {
        "n_files": len(files),
        "lines_added": added,
        "lines_deleted": deleted,
        "log_churn": math.log1p(added + deleted),
        "n_functions_changed": sum(len(f.functions_changed) for f in files),
        "n_modules": len({str(PurePosixPath(f.path).parent) for f in src}),
        "n_src_files": len(src),
        "n_test_files": len(tests),
        "n_docs_files": len(docs),
        "n_dependency_files": sum(f.is_dependency for f in files),
        "n_config_files": sum(f.is_config for f in files),
        "ci_config_changed": float(any(".github" in PurePosixPath(f.path).parts for f in files)),
        "n_db_related": sum(bool(DB_RE.search(f.path)) for f in files),
        "n_security_related": sum(bool(SECURITY_RE.search(f.path)) for f in files),
        "src_without_tests": float(len(src) > 0 and len(tests) == 0),
        "docs_only": float(len(files) > 0 and len(docs) == len(files)),
        "is_pull_request": float(is_pull_request),
        "is_merge": float(is_merge),
        "msg_mentions_fix": float(bool(FIX_RE.search(message))),
    }


def build_features(
    diff: DiffSummary,
    message: str = "",
    history: HistoryContext | None = None,
    is_pull_request: bool = False,
    is_merge: bool = False,
) -> dict[str, float]:
    feats = diff_features(diff, message, is_pull_request, is_merge)
    feats.update(asdict(history or HistoryContext()))
    return {k: float(feats[k]) for k in ALL_FEATURES}
