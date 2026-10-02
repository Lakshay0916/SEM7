"""Load the trained risk model and assess a commit.

Risk is an additional signal, not proof of a bug. Risk factors are a heuristic
explanation: features that are unusually high for this commit (vs. training
data), that correlate with failure, weighted by permutation importance.
"""

from __future__ import annotations

from pathlib import Path

import joblib
import pandas as pd

from app.config import PROJECT_ROOT
from app.models.schemas import DiffSummary, RiskAssessment, RiskLevel
from app.risk.features import HistoryContext, build_features
from app.risk.train import level_of

MODEL_PATH = PROJECT_ROOT / "models" / "risk_model.joblib"

FACTOR_TEXT = {
    "n_files": "{v:.0f} files changed",
    "lines_added": "{v:.0f} lines added",
    "lines_deleted": "{v:.0f} lines deleted",
    "log_churn": "large overall churn",
    "n_functions_changed": "{v:.0f} functions/classes modified",
    "n_modules": "touches {v:.0f} source directories",
    "n_src_files": "{v:.0f} source files changed",
    "n_test_files": "{v:.0f} test files changed",
    "n_docs_files": "{v:.0f} documentation files changed",
    "n_dependency_files": "dependency manifest changed ({v:.0f} files)",
    "n_config_files": "configuration changed ({v:.0f} files)",
    "ci_config_changed": "CI workflow configuration changed",
    "n_db_related": "database/migration-related files changed ({v:.0f})",
    "n_security_related": "security/auth-related files changed ({v:.0f})",
    "src_without_tests": "source changed without accompanying test changes",
    "docs_only": "documentation-only change",
    "is_pull_request": "pull-request commit",
    "is_merge": "merge commit",
    "msg_mentions_fix": "commit message indicates a fix",
    "hist_file_max_failure_rate": "a touched file has a {v:.0%} historical CI failure rate",
    "hist_file_mean_failure_rate": "touched files average a {v:.0%} historical CI failure rate",
    "hist_file_mean_prior_changes": "frequently changed files (avg {v:.1f} prior changes)",
    "author_prior_commits": "author has {v:.0f} prior CI-tested commits",
    "author_failure_rate": "author's prior commits failed CI {v:.0%} of the time",
    "parent_known": "parent commit CI result known",
    "parent_failed": "parent commit failed CI",
    "repo_recent_failure_rate": "repository's recent CI failure rate is {v:.0%}",
}


class RiskPredictor:
    def __init__(self, bundle: dict) -> None:
        self.bundle = bundle
        self.version = bundle["version"]

    @classmethod
    def load(cls, path: str | Path = MODEL_PATH) -> "RiskPredictor":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"risk model not found at {path}; run scripts/train_risk_model.py first"
            )
        return cls(joblib.load(path))

    def explain(self, feats: dict[str, float], variant: str, top_k: int = 5) -> list[str]:
        v = self.bundle["variants"][variant]
        scored = []
        for f in v["features"]:
            val, imp, q = feats[f], v["importance"].get(f, 0.0), v["quantiles"][f]
            if imp <= 0 or v["direction"][f] < 0 or val <= 0 or val <= q[75]:
                continue
            spread = max(q[90] - q[50], 1e-9)
            scored.append((imp * min((val - q[50]) / spread, 3.0), f, val, q[50]))
        scored.sort(reverse=True)
        return [
            f"{FACTOR_TEXT.get(f, f).format(v=val)} (typical: {med:g})"
            for _, f, val, med in scored[:top_k]
        ]

    def assess(
        self,
        diff: DiffSummary,
        message: str = "",
        history: HistoryContext | None = None,
        is_pull_request: bool = False,
        is_merge: bool = False,
        commit_sha: str | None = None,
    ) -> RiskAssessment:
        variant = "full" if history is not None else "diff_only"
        v = self.bundle["variants"][variant]
        feats = build_features(diff, message, history, is_pull_request, is_merge)
        X = pd.DataFrame([feats])[v["features"]]
        score = float(v["model"].predict_proba(X)[0, 1])
        return RiskAssessment(
            commit_sha=commit_sha or diff.head_sha,
            risk_score=round(score, 4),
            risk_level=RiskLevel(level_of(score, v["risk_levels"])),
            risk_factors=self.explain(feats, variant),
            features={k: feats[k] for k in v["features"]},
            model_version=f"{self.version}:{variant}:{v['chosen_model']}",
        )
