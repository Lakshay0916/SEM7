from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.models.schemas import DiffSummary, FileChange, RiskLevel
from app.risk.features import ALL_FEATURES, DIFF_FEATURES, HistoryContext, build_features, diff_features
from app.risk.mining import CommitLabel, RepoSpec, aggregate_labels, build_rows, run_label
from app.risk.predictor import RiskPredictor
from app.risk.train import temporal_split, train_all

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _diff(*files: FileChange) -> DiffSummary:
    return DiffSummary(base_sha="a", head_sha="b", files=list(files))


# ------------------------------------------------------------------ features


def test_diff_features_counts():
    d = _diff(
        FileChange(path="src/auth/login.py", change_type="M", additions=10, deletions=2,
                   functions_changed=["login", "logout"]),
        FileChange(path="requirements.txt", change_type="M", additions=1, deletions=0, is_dependency=True),
        FileChange(path=".github/workflows/ci.yml", change_type="M", additions=3, is_config=True),
    )
    f = diff_features(d, "Fix login bug")
    assert f["n_files"] == 3 and f["lines_added"] == 14 and f["lines_deleted"] == 2
    assert f["n_functions_changed"] == 2
    assert f["n_src_files"] == 1 and f["n_test_files"] == 0
    assert f["n_dependency_files"] == 1 and f["n_config_files"] == 1
    assert f["ci_config_changed"] == 1.0
    assert f["n_security_related"] == 1
    assert f["src_without_tests"] == 1.0
    assert f["msg_mentions_fix"] == 1.0
    assert f["docs_only"] == 0.0


def test_docs_only_and_feature_order():
    d = _diff(FileChange(path="docs/index.rst", change_type="M", additions=5))
    f = build_features(d)
    assert list(f) == ALL_FEATURES
    assert f["docs_only"] == 1.0 and f["src_without_tests"] == 0.0
    assert all(f[h] == 0.0 for h in ALL_FEATURES if h not in DIFF_FEATURES)  # no history -> zeros


# ------------------------------------------------------------------ labels


def _run(conclusion, jobs=None, event="push", sha="s1", created=T0):
    return {"id": 1, "head_sha": sha, "event": event, "status": "completed", "conclusion": conclusion,
            "created_at": created.isoformat(), "updated_at": (created + timedelta(minutes=5)).isoformat(),
            "jobs": jobs}


def _job(name, steps, conclusion="failure"):
    return {"name": name, "conclusion": conclusion, "failed_steps": steps}


def test_run_label_rules():
    assert run_label(_run("success")) == 0
    assert run_label(_run("cancelled")) is None
    assert run_label(_run("failure", jobs=None)) is None  # no evidence -> unusable
    assert run_label(_run("failure", [_job("3.12", ["Run tox"])])) == 1
    assert run_label(_run("failure", [_job("Development Versions", ["Run tox"])])) == 0
    assert run_label(_run("failure", [_job("3.12", ["Set up Python"])])) is None  # infra only
    assert run_label(_run("success", event="schedule")) is None


def test_aggregate_any_genuine_failure_wins():
    runs = [_run("success"), _run("failure", [_job("3.12", ["Run pytest"])], event="pull_request")]
    lab = aggregate_labels(runs)["s1"]
    assert lab.label == 1 and lab.is_pr and lab.n_runs == 2


# ------------------------------------------------------------------ leakage


def test_history_only_uses_results_known_before(git_project):
    repo = git_project
    shas = []
    for i in range(3):
        (repo.path / "src/mod.py").write_text(f"def f():\n    return {i + 2}\n")
        shas.append(repo.commit_all(f"c{i}"))

    def lab(sha, created_min, known_min, y):
        return CommitLabel(sha=sha, label=y, created_at=T0 + timedelta(minutes=created_min),
                           known_at=T0 + timedelta(minutes=known_min), is_pr=False, n_runs=1)

    # c0 fails but its CI finishes at t=50, after c1's CI starts (t=10) and before c2's (t=60).
    labels = [lab(shas[0], 0, 50, 1), lab(shas[1], 10, 20, 0), lab(shas[2], 60, 70, 0)]
    rows = {r["sha"]: r for r in build_rows(RepoSpec("x/y", []), repo.path, labels, workers=1)}

    assert rows[shas[1]]["parent_known"] == 0.0  # c0's result unknown when c1 was tested
    assert rows[shas[1]]["repo_recent_failure_rate"] == 0.0
    assert rows[shas[2]]["parent_known"] == 1.0  # c1 known by t=60
    assert rows[shas[2]]["parent_failed"] == 0.0
    assert rows[shas[2]]["repo_recent_failure_rate"] == 0.5  # c0 (fail) + c1 (pass)
    assert rows[shas[2]]["hist_file_max_failure_rate"] == 0.5  # src/mod.py: 1 fail / 2 changes


# ------------------------------------------------------------------ training / prediction


def _synthetic(n=600, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for repo in ("a/a", "b/b"):
        for i in range(n // 2):
            feats = {f: float(rng.poisson(2)) for f in ALL_FEATURES}
            feats["parent_failed"] = float(rng.random() < 0.3)
            logit = -2 + 0.4 * feats["n_files"] + 1.5 * feats["parent_failed"]
            rows.append({"repo": repo, "sha": f"{repo}{i}", "created_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
                         "label": int(rng.random() < 1 / (1 + np.exp(-logit))), "n_runs": 1, **feats})
    return pd.DataFrame(rows)


def test_temporal_split_is_per_repo_and_ordered():
    df = temporal_split(_synthetic())
    for _, g in df.groupby("repo"):
        assert g[g.split == "train"].created_at.max() < g[g.split == "val"].created_at.min()
        assert g[g.split == "val"].created_at.max() < g[g.split == "test"].created_at.min()
        assert abs((g.split == "test").mean() - 0.2) < 0.01


@pytest.fixture(scope="module")
def trained():
    return train_all(_synthetic())


def test_training_report_has_real_metrics(trained):
    _, report = trained
    for v in ("full", "diff_only"):
        r = report["variants"][v]
        assert r["chosen_model"] in r["candidates"]
        assert 0.0 <= r["candidates"][r["chosen_model"]]["test"]["roc_auc"] <= 1.0
        assert "majority_class (always pass)" in r["baselines_test"]


def test_predictor_assess(trained):
    bundle, _ = trained
    pred = RiskPredictor(bundle)
    d = _diff(*(FileChange(path=f"src/m{i}.py", change_type="M", additions=20) for i in range(15)))
    no_hist = pred.assess(d, "big refactor")
    assert no_hist.model_version.split(":")[1] == "diff_only"
    assert isinstance(no_hist.risk_level, RiskLevel) and 0 <= no_hist.risk_score <= 1
    assert any("files changed" in f for f in no_hist.risk_factors)

    with_hist = pred.assess(d, history=HistoryContext(parent_known=1, parent_failed=1))
    assert with_hist.model_version.split(":")[1] == "full"
