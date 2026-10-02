# SEM7 — AI Self-Healing CI/CD + Agentic Debugging

ML-assisted multi-agent system that predicts code-change risk, investigates CI
failures with RAG and specialised agents, proposes a repair for human approval,
applies it on a separate `ai/repair/*` branch, validates it with real tests,
reflects on failed repairs, and opens a PR only after validation.

> ML predicts, RAG retrieves, LLM reasons, agents act, and tests verify.

One codebase serves two tracks:

| Track | Focus | State |
|---|---|---|
| **IDT / DevOps** | Risk-aware quality gate, real CI tests, human-approved self-healing loop, Docker, GitHub Actions, reports | **implemented** |
| **Agentic AI** | RAG retrieval and LLM agents for investigation, root cause and planning | next — plugs into the same `RepairStrategy` interface and loop |

![CI](https://github.com/Lakshay0916/SEM7/actions/workflows/ci.yml/badge.svg)

## Setup

```bash
conda create -n SEM7 python=3.11 -y
conda activate SEM7
pip install -r requirements.txt
cp .env.example .env   # optional; defaults work offline
```

Or with Docker (no local Python needed):

```bash
docker compose up                                # dashboard at http://localhost:8502
docker compose --profile tools run --rm tests    # test suite inside the container
docker compose --profile tools run --rm cli python scripts/run_ci.py --scenario failed_first_repair --heal --approve
```

## Run

```bash
streamlit run ui/app.py                          # dashboard at http://localhost:8502
python scripts/run_ci.py --list                  # available demo scenarios
python scripts/run_ci.py --scenario simple_bug   # sandbox -> commit -> diff -> risk -> real tests -> FailureEvent
python scripts/run_ci.py --scenario failed_first_repair --heal            # propose a fix, stop for approval
python scripts/run_ci.py --scenario failed_first_repair --heal --approve  # approve every plan up front
python scripts/ci_gate.py --base origin/main     # risk-aware quality gate on this repo (what CI runs)
python scripts/export_report.py --latest         # Markdown report of the latest workflow
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
| 5, 7 | Investigation, root cause, solution planning | done — **rule-based** `RepairStrategy` (LLM agents next) |
| 6 | RAG retrieval | todo (Agentic AI) |
| 8–11 | Human approval, repair on `ai/repair/*`, real re-test, bounded reflection | done |
| 12 | PR adapter | done — local adapter (GitHub adapter todo) |
| IDT | Quality gate, Docker, GitHub Actions (Linux + Windows), Markdown reports | done |

## DevOps / IDT pipeline

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | What it does |
|---|---|
| Quality gate (Linux) | `scripts/ci_gate.py`: diff vs. base → ML risk score → **full test suite** → gate. Report in the run summary, JSON artifact. |
| Tests (Windows) | Full suite on Windows (cross-platform paths, encodings, line endings). |
| Docker | Builds the image, runs the tests inside it, smoke-tests the dashboard's health endpoint. |
| Self-healing demo | Scenario 3 end to end (fix 1 fails → reflection → fix 2 → PR); uploads the report and PR description. |

**Gate policy** (`app/ci/quality_gate.py`) — tests always run; risk can only add scrutiny:
FAIL / ERROR / TIMEOUT → **BLOCK** (red check) · tests pass + HIGH risk → **REVIEW** (warning) · otherwise **PASS**.
Risk never skips tests: the diff-only model is a weak signal (test ROC-AUC 0.60).

## Self-healing loop

```
FAILED → INVESTIGATING → ROOT_CAUSE_IDENTIFIED → SOLUTION_PROPOSED → WAITING_APPROVAL
      → (human) APPROVED → REPAIRING (ai/repair/<id>) → TESTING_REPAIR → PASSED → PR_CREATED
                                                    ↘ REFLECTING → INVESTIGATING …  (max 3 attempts)
      → (human) REJECTED → re-analyse or stop
```

- `app/healing/strategy.py` — the `RepairStrategy` interface: `investigate()`, `root_cause()`, `plan()`.
  These map one-to-one onto the planned Investigation, Root-Cause and Planner agents.
- `app/healing/rules.py` — today's **rule-based** strategy (no LLM): restore a changed operator first;
  if tests still fail, restore the implicated function from `main`.
- `app/healing/loop.py` — strategy-agnostic loop: approval, repair branch, real re-test, reflection, PR.
- `app/healing/patcher.py` — applies only approved plans; refuses tests/config/dependency edits and ambiguous snippets.

Adding the Agentic AI track means writing an LLM-backed class with the same three methods; the
approval gate, safety checks, test verification, reflection and PR flow are reused unchanged.

## Safety guarantees

- `GitRepo(actor="ai")` cannot commit/reset/push `main`/`master` and may only
  create branches prefixed `ai/repair/` (`app/git/repo.py`).
- The test runner reports `PASS` only when pytest exits 0, ≥1 test ran, and no
  failures/errors were parsed from its JUnit report. Timeouts, collection
  errors, missing reports and zero-test runs are `TIMEOUT`/`ERROR`.
- Test targets are passed as argv (no shell) and must stay inside the project.
- Secrets are read from env only and excluded from `Settings` repr.
- No repair without a recorded human approval (state machine: only `APPROVED → REPAIRING`).
- Repairs may not modify tests, CI config or dependency files; each edit must match exactly once.
- Retries are bounded by `MAX_REPAIR_ATTEMPTS`; PRs are never merged automatically.

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
  ci/quality_gate.py  PASS / REVIEW / BLOCK policy
  healing/            RepairStrategy interface, rule-based strategy, safe patcher, loop, PR adapter
  reporting/          Markdown reports (PR body, workflow report, CI gate summary)
  orchestration/      workflow state machine, SQLite store, CI workflow orchestrator
  logging_utils.py    secret-redacting logging, stage timing
demo_projects/calculator/   controlled demo target
ui/                         Streamlit dashboard (app.py + views/)
scripts/                    run_ci.py, ci_gate.py, export_report.py, mine_ci_data.py, train_risk_model.py
.github/workflows/ci.yml    GitHub Actions pipeline
Dockerfile, docker-compose.yml
data/risk/                  repos.json, mined runs/rows, commits.csv, evaluation
models/risk_model.joblib    trained risk model
tests/                      unit + safety tests
```
