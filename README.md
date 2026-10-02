# SEM7 — AI Self-Healing CI/CD + Agentic Debugging

ML-assisted multi-agent system that predicts code-change risk, investigates CI
failures with RAG and specialised agents, proposes a repair for human approval,
applies it on a separate `ai/repair/*` branch, validates it with real tests,
reflects on failed repairs, and opens a PR only after validation.

> ML predicts, RAG retrieves, LLM reasons, agents act, and tests verify.

## Setup

```bash
conda create -n SEM7 python=3.11 -y
conda activate SEM7
pip install -r requirements.txt
cp .env.example .env   # optional; defaults work offline
```

## Run

```bash
streamlit run ui/app.py                          # dashboard at http://localhost:8502
python scripts/run_ci.py --list                  # available demo scenarios
python scripts/run_ci.py --scenario simple_bug   # sandbox -> commit -> diff -> risk -> real tests -> FailureEvent
pytest                                           # project test suite

# ML risk model (dataset + model are committed; these regenerate them)
python scripts/mine_ci_data.py                   # mine CI outcomes from public repos (needs `gh auth login` or GITHUB_TOKEN)
python scripts/train_risk_model.py               # train/evaluate -> models/risk_model.joblib, data/risk/EVALUATION.md
```

Sandboxes are created under `workspace/runs/<id>/repo` (git-ignored). Each has a
healthy `main` and a feature branch carrying the scenario's buggy commit.

## Status

| Phase | Component | State |
|---|---|---|
| 1 | Scaffold, config, typed schemas | done |
| 2 | Git wrapper, diff extraction, test runner, CI simulator | done |
| 3 | ML risk module (mined from 9 public repos) | done — see `data/risk/EVALUATION.md` |
| 4 | Workflow state machine + SQLite persistence + redacted logging | done |
| UI | Streamlit dashboard (Dashboard, Run Pipeline, Workflows, Risk Model) | done (extended each phase) |
| 5–7 | Investigation, RAG, Root Cause, Solution Planner | todo |
| 8–11 | Approval, Repair, Testing, Reflection | todo |
| 12–14 | PR adapter, UI, evaluation | todo |

## Safety guarantees implemented so far

- `GitRepo(actor="ai")` cannot commit/reset/push `main`/`master` and may only
  create branches prefixed `ai/repair/` (`app/git/repo.py`).
- The test runner reports `PASS` only when pytest exits 0, ≥1 test ran, and no
  failures/errors were parsed from its JUnit report. Timeouts, collection
  errors, missing reports and zero-test runs are `TIMEOUT`/`ERROR`.
- Test targets are passed as argv (no shell) and must stay inside the project.
- Secrets are read from env only and excluded from `Settings` repr.

## ML risk module

Dataset: 3,625 commits from 9 public Python repos (flask, click, httpx, rich,
requests, alembic, attrs, pip-tools, poetry), labelled with each repo's real
GitHub Actions *test-workflow* outcome judged per job (dev-dependency jobs
ignored, infra-only failures dropped). 8.9% genuine failures.

Protocol: temporal split per repo (70/10/20), model + threshold chosen on
validation, test reported against naive baselines, plus leave-one-repo-out.

| Variant | Chosen model | Test ROC-AUC | Test PR-AUC | Best baseline PR-AUC |
|---|---|---|---|---|
| `full` (diff + CI history) | logistic regression | 0.663 | 0.232 | 0.171 (repo recent failure rate) |
| `diff_only` (no history; used for demo sandbox) | random forest | 0.604 | 0.161 | 0.120 (base rate) |

Observed test failure rate by predicted level (`full`): LOW 5.8% · MEDIUM 14.5% · HIGH 22.0%.
The signal is real but modest — risk is an extra signal, not proof of a bug.
Full report: [data/risk/EVALUATION.md](data/risk/EVALUATION.md).

## Layout

```
app/
  config.py           settings from env/.env
  models/schemas.py   typed contracts (FailureEvent, TestReport, RootCauseReport, ...)
  git/repo.py         safe git wrapper (protected-branch guard)
  git/diff.py         diff -> DiffSummary (per-file stats, AST-detected changed functions)
  ci/test_runner.py   real pytest execution -> TestReport
  ci/scenarios.py     controlled demo scenarios
  ci/simulator.py     sandbox + local CI pipeline -> PipelineRun / FailureEvent
  risk/features.py    commit -> features (shared by mining and prediction)
  risk/mining.py      GitHub Actions history -> labelled, leakage-free dataset
  risk/train.py       models, temporal split, metrics, baselines
  risk/predictor.py   RiskAssessment with explained risk factors
  orchestration/      workflow state machine, SQLite store, CI workflow orchestrator
  logging_utils.py    secret-redacting logging, stage timing
demo_projects/calculator/   controlled demo target
ui/                         Streamlit dashboard (app.py + views/)
scripts/                    run_ci.py, mine_ci_data.py, train_risk_model.py
data/risk/                  repos.json, mined runs/rows, commits.csv, evaluation
models/risk_model.joblib    trained risk model
tests/                      unit + safety tests
```
