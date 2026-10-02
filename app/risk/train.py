"""Train and evaluate commit-risk models on the mined dataset.

Protocol (no future leakage):
* per repository, commits are ordered by CI time; oldest 70% -> train,
  next 10% -> validation, newest 20% -> test
* model choice and decision threshold are selected on validation only
* test metrics are reported for every candidate and for naive baselines
* a leave-one-repo-out run measures cross-project generalisation

Two variants are trained: "full" (diff + CI-history features) and
"diff_only" (for commits with no CI history, e.g. the demo sandbox).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.base import clone
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

from app.risk.features import ALL_FEATURES, DIFF_FEATURES

log = logging.getLogger(__name__)
SEED = 42
VARIANTS = {"full": ALL_FEATURES, "diff_only": DIFF_FEATURES}


def candidate_models() -> dict:
    return {
        "logistic_regression": make_pipeline(
            FunctionTransformer(np.log1p),  # counts are heavy-tailed; all features >= 0
            StandardScaler(),
            LogisticRegression(class_weight="balanced", max_iter=5000, C=0.5),
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=400, min_samples_leaf=5, class_weight="balanced_subsample",
            n_jobs=-1, random_state=SEED,
        ),
        "gradient_boosting": HistGradientBoostingClassifier(
            class_weight="balanced", max_iter=300, learning_rate=0.05, max_leaf_nodes=15,
            l2_regularization=1.0, random_state=SEED,
        ),
    }


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def temporal_split(df: pd.DataFrame, val_frac: float = 0.1, test_frac: float = 0.2) -> pd.DataFrame:
    df = df.sort_values(["repo", "created_at"]).copy()
    df["split"] = "train"
    for _, idx in df.groupby("repo").groups.items():
        n = len(idx)
        n_test, n_val = int(round(n * test_frac)), int(round(n * val_frac))
        idx = list(idx)
        df.loc[idx[n - n_test:], "split"] = "test"
        df.loc[idx[n - n_test - n_val : n - n_test], "split"] = "val"
    return df


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def best_f1_threshold(y: np.ndarray, score: np.ndarray) -> float:
    prec, rec, thr = precision_recall_curve(y, score)
    f1 = 2 * prec[:-1] * rec[:-1] / np.clip(prec[:-1] + rec[:-1], 1e-12, None)
    return float(thr[int(np.nanargmax(f1))]) if len(thr) else 0.5


def metrics(y: np.ndarray, score: np.ndarray, threshold: float) -> dict:
    pred = (score >= threshold).astype(int)
    both = len(np.unique(y)) > 1
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "n": int(len(y)),
        "positives": int(y.sum()),
        "threshold": round(float(threshold), 4),
        "accuracy": round(accuracy_score(y, pred), 4),
        "precision": round(precision_score(y, pred, zero_division=0), 4),
        "recall": round(recall_score(y, pred, zero_division=0), 4),
        "f1": round(f1_score(y, pred, zero_division=0), 4),
        "roc_auc": round(roc_auc_score(y, score), 4) if both else None,
        "pr_auc": round(average_precision_score(y, score), 4) if both else None,
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def risk_level_thresholds(val_scores: np.ndarray, medium_q: float = 0.50, high_q: float = 0.85) -> dict:
    """Risk levels by rank among validation commits: LOW = bottom half,
    MEDIUM = 50th-85th percentile, HIGH = top 15%. Rank-based levels stay
    meaningful even when predicted probabilities are poorly calibrated."""
    return {
        "medium": round(float(np.quantile(val_scores, medium_q)), 4),
        "high": round(float(np.quantile(val_scores, high_q)), 4),
    }


def level_of(score: float, thresholds: dict) -> str:
    if score >= thresholds["high"]:
        return "HIGH"
    if score >= thresholds["medium"]:
        return "MEDIUM"
    return "LOW"


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #


def _scores(model, X: pd.DataFrame) -> np.ndarray:
    return model.predict_proba(X)[:, 1]


def train_variant(df: pd.DataFrame, features: list[str]) -> dict:
    tr, va, te = (df[df.split == s] for s in ("train", "val", "test"))
    Xtr, ytr = tr[features], tr.label.values
    Xva, yva = va[features], va.label.values
    Xte, yte = te[features], te.label.values

    candidates = {}
    for name, est in candidate_models().items():
        model = clone(est).fit(Xtr, ytr)
        s_val = _scores(model, Xva)
        thr = best_f1_threshold(yva, s_val)
        candidates[name] = {
            "model": model,
            "threshold": thr,
            "val": metrics(yva, s_val, thr),
            "test": metrics(yte, _scores(model, Xte), thr),
        }
        log.info("%s val PR-AUC=%s test PR-AUC=%s", name, candidates[name]["val"]["pr_auc"],
                 candidates[name]["test"]["pr_auc"])

    chosen = max(candidates, key=lambda n: candidates[n]["val"]["pr_auc"] or 0)
    best = candidates[chosen]

    # Naive baselines on the same test set.
    prior = ytr.mean()
    baselines = {
        "majority_class (always pass)": metrics(yte, np.zeros(len(yte)), 0.5),
        "random (train failure rate)": metrics(
            yte, np.random.default_rng(SEED).random(len(yte)), 1 - prior
        ),
    }
    if "parent_failed" in features:
        baselines["rule: parent commit failed"] = metrics(yte, te.parent_failed.values, 0.5)
    if "repo_recent_failure_rate" in features:
        rr_thr = best_f1_threshold(yva, va.repo_recent_failure_rate.values)
        baselines["rule: repo recent failure rate"] = metrics(yte, te.repo_recent_failure_rate.values, rr_thr)

    # Explanation support: permutation importance (validation), direction, train quantiles.
    imp = permutation_importance(
        best["model"], Xva, yva, scoring="average_precision", n_repeats=10, random_state=SEED, n_jobs=-1
    )
    importance = {f: round(max(float(m), 0.0), 5) for f, m in zip(features, imp.importances_mean)}
    direction = {}
    for f in features:
        rho = spearmanr(Xtr[f], ytr).statistic if Xtr[f].nunique() > 1 else 0.0
        direction[f] = 1 if (rho or 0) > 0 else -1
    quantiles = {f: {q: float(Xtr[f].quantile(q / 100)) for q in (50, 75, 90)} for f in features}

    s_val_best = _scores(best["model"], Xva)
    s_te_best = _scores(best["model"], Xte)
    levels = risk_level_thresholds(s_val_best)
    level_rates = {}
    te_levels = np.array([level_of(s, levels) for s in s_te_best])
    for lvl in ("LOW", "MEDIUM", "HIGH"):
        mask = te_levels == lvl
        level_rates[lvl] = {
            "commits": int(mask.sum()),
            "observed_failure_rate": round(float(yte[mask].mean()), 4) if mask.any() else None,
        }

    per_repo = {}
    for repo, g in te.groupby("repo"):
        per_repo[repo] = metrics(g.label.values, _scores(best["model"], g[features]), best["threshold"])

    return {
        "features": features,
        "chosen_model": chosen,
        "model": best["model"],
        "threshold": best["threshold"],
        "risk_levels": levels,
        "importance": importance,
        "direction": direction,
        "quantiles": quantiles,
        "report": {
            "chosen_model": chosen,
            "selection": "highest validation PR-AUC",
            "candidates": {n: {"val": c["val"], "test": c["test"]} for n, c in candidates.items()},
            "baselines_test": baselines,
            "test_failure_rate_by_risk_level": level_rates,
            "test_per_repo": per_repo,
            "permutation_importance_val": dict(sorted(importance.items(), key=lambda kv: -kv[1])),
        },
    }


def leave_one_repo_out(df: pd.DataFrame, features: list[str], model_name: str) -> dict:
    out = {}
    for repo in sorted(df.repo.unique()):
        tr, te = df[df.repo != repo], df[df.repo == repo]
        if te.label.nunique() < 2:
            continue
        model = clone(candidate_models()[model_name]).fit(tr[features], tr.label.values)
        s = _scores(model, te[features])
        out[repo] = {
            "n": int(len(te)),
            "failure_rate": round(float(te.label.mean()), 4),
            "roc_auc": round(roc_auc_score(te.label, s), 4),
            "pr_auc": round(average_precision_score(te.label, s), 4),
        }
    return out


def train_all(df: pd.DataFrame) -> tuple[dict, dict]:
    """Returns (model_bundle, evaluation_report)."""
    df = temporal_split(df)
    version = "risk-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    bundle = {"version": version, "variants": {}}
    report = {
        "version": version,
        "dataset": {
            "rows": int(len(df)),
            "repos": sorted(df.repo.unique().tolist()),
            "failure_rate": round(float(df.label.mean()), 4),
            "split_sizes": df.split.value_counts().to_dict(),
            "split_failure_rates": df.groupby("split").label.mean().round(4).to_dict(),
        },
        "variants": {},
    }
    for variant, feats in VARIANTS.items():
        log.info("training variant %s (%d features)", variant, len(feats))
        res = train_variant(df, feats)
        res["report"]["leave_one_repo_out"] = leave_one_repo_out(df, feats, res["chosen_model"])
        report["variants"][variant] = res.pop("report")
        bundle["variants"][variant] = res
    return bundle, report
