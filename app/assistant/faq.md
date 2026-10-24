# SEM7 assistant knowledge base

Curated answers about the project. Each `##` heading is one retrievable entry.
Keep answers factual: numbers must match `data/risk/EVALUATION.md` and the code.

## What is this project and what does it do?
SEM7 is an AI-powered self-healing CI/CD system. When a commit breaks the tests, it scores how
risky the change was with an ML model, runs the real test suite, packages the failure evidence,
investigates the root cause and proposes a fix. Nothing changes until a human approves. After
approval it repairs the code on a separate `ai/repair/*` branch, re-runs the real tests, reflects
and retries if the fix was incomplete (at most 3 attempts), and opens a pull request that is never
merged automatically. The project serves two courses: Agentic AI (agents, RAG, LLMs) and IDT /
DevOps (CI/CD, containers, cloud deployment).

## What are the steps of the pipeline?
1. Commit received - a sandbox copy of the demo calculator project gets a scenario's commit.
2. Change analysis - the diff is extracted (files, lines, changed functions via the AST).
3. ML risk prediction - the risk model scores the commit LOW / MEDIUM / HIGH before tests run.
4. CI test run - pytest really runs; results are parsed from JUnit XML.
5. Quality gate - PASS, REVIEW or BLOCK from the test result plus the risk level.
6. Investigation and root cause - failing tests, error messages and the diff are analysed.
7. Solution plan - a concrete code change is proposed (no code is changed yet).
8. Human approval - you approve, reject or abort.
9. Repair - the plan is applied and committed on `ai/repair/<workflow-id>`.
10. Re-test - the full test suite runs again on the repair branch.
11. Reflection - if tests still fail, the system compares before/after and plans a new attempt.
12. Pull request - after a passing re-test a PR description is produced (never auto-merged).

## What are the three demo scenarios?
- Scenario 1 `simple_bug`: `add()` is changed to subtract, so `test_add` fails. One repair attempt fixes it.
- Scenario 2 `high_risk_change`: a broad commit (several functions rewritten, a dependency manifest,
  test config and a security-related module added, no test changes). Tests still pass, but risk is HIGH,
  so the quality gate says REVIEW. It shows that risk is a signal, not proof of a bug.
- Scenario 3 `failed_first_repair`: `divide()` is "simplified" to floor division and its zero check is
  removed. Fix 1 restores the operator but one test still fails, so the system reflects and fix 2
  restores the whole function. This demonstrates the reflection loop.

## What is the quality gate and how does it decide?
The quality gate (`app/ci/quality_gate.py`) combines the real test result with the ML risk level.
Tests always run; risk can only add scrutiny, never skip tests.
- Tests FAIL, ERROR or TIMEOUT -> BLOCK (red check).
- Tests pass and risk is HIGH -> REVIEW (warning, a human should look).
- Otherwise -> PASS.

## What happens when I approve a fix? Is a real PR created?
When you approve, the system creates the branch `ai/repair/<workflow-id>` inside that run's sandbox
git repository, applies the plan, makes a real git commit, and re-runs the 9 calculator tests. If they
pass it "opens a PR" with `LocalPRAdapter`, which writes `PR_ai_repair_<workflow-id>.md` next to the
sandbox repo (title, branch -> main, root cause, fix, test evidence, "not merged"). Nothing is pushed to
GitHub, because the sandbox repos have no remote. A GitHub PR adapter can plug into the same
`PRAdapter` interface later. `main` is never changed and nothing is merged.

## What happens if I reject or abort?
Reject records your decision and stops the workflow in state REJECTED; no code is changed. Abort stops
a workflow at any point with a reason (state ABORTED). Both are saved in the workflow history.

## How many repair attempts are allowed and what is reflection?
At most 3 attempts (`MAX_REPAIR_ATTEMPTS`). After each attempt the full test suite runs. If tests still
fail, the reflection step compares the before/after results: which tests were newly fixed, which still
fail, and whether anything regressed. That evidence feeds a new investigation and a stronger plan
(the rule-based strategy escalates from operator-restore to restore-function). Each new plan needs a
new human approval.

## Can the AI modify the main branch?
No. The git wrapper (`app/git/repo.py`) refuses any commit, checkout or reset by the AI actor on
protected branches (`main`, `master`) and raises `ProtectedBranchError`. The AI may only write to
branches starting with `ai/repair/`. The safety check button tries an AI commit on main and shows that
it is blocked and main is unchanged. Patches that touch tests, config or dependencies are also rejected.

## What are the 9 tests? Are they different from the GitHub Actions tests?
There are two separate test sets.
- The 9 calculator tests (`demo_projects/calculator/tests/test_calculator.py`) test the demo app that the
  pipeline breaks and repairs: add, add negative, subtract, multiply, divide, divide negative, divide by
  zero, average and average of an empty list. They are the evidence for whether a repair worked.
- The project's own test suite (`tests/`, about 114 tests in 12 files) tests the system itself: git
  safety, diff extraction, test runner, scenarios, risk model, healing loop, database store, API, UI and
  this assistant.
GitHub Actions runs both: the project tests on Linux (with PostgreSQL) and Windows and inside the Docker
image, and the calculator tests during the self-healing demo job.

## How was the ML risk model trained? Which repos were mined for data?
The data is real GitHub Actions history mined from 9 public Python repos (rich, httpx,
pip-tools, click, flask, requests, attrs, poetry, alembic): 3,625 commits with an overall CI failure
rate of 8.9%. Labels come from job/step-level CI results with noisy jobs (nightly, dev-version,
allow-failure) filtered out. The split is temporal per repo (70% train, 10% validation, 20% test), so
the model learns from older commits and is tested on newer ones. Three models were trained per feature
set (Logistic Regression, Random Forest, Gradient Boosting) and the winner was chosen by validation
PR-AUC, never by the test set.

## How good is the risk model? What are the metrics?
Full model (Logistic Regression, all 27 features), test set: ROC-AUC 0.663, PR-AUC 0.232 (random
guessing scores about 0.12). Diff-only model (Random Forest, 19 features): test ROC-AUC 0.604,
PR-AUC 0.161. Observed test failure rate by predicted level (full model): LOW 5.8%, MEDIUM 14.5%,
HIGH 22.0%, so HIGH-risk commits fail almost 4x as often as LOW ones. Risk levels are set from
validation score quantiles (p50 and p85). The model is a useful but weak signal, which is why tests
always run regardless of risk.

## Why Logistic Regression for one variant and Random Forest for the other?
The model was not hand-picked: for each variant the training script trained three models and kept the
one with the highest validation PR-AUC. Full variant: Logistic Regression won (0.528 vs 0.495 RF and
0.508 GB) because the strongest history features (repo recent failure rate, parent commit failed) have
simple, roughly linear effects and there are only about 320 failures, so a simple model overfits less.
Diff-only variant: Random Forest won (0.270 vs 0.195 LR and 0.226 GB) because without history the risk
comes from combinations of weak signals (for example source changed AND no tests AND many files),
which trees capture. On the test set the models are nearly tied, so features matter more than the
algorithm.

## What are the 27 features of the risk model?
19 diff features computed from the commit itself: n_files, lines_added, lines_deleted, log_churn,
n_functions_changed, n_modules, n_src_files, n_test_files, n_docs_files, n_dependency_files,
n_config_files, ci_config_changed, n_db_related, n_security_related, src_without_tests, docs_only,
is_pull_request, is_merge, msg_mentions_fix.
8 history features computed only from CI results known before the commit was tested (no leakage):
hist_file_max_failure_rate, hist_file_mean_failure_rate, hist_file_mean_prior_changes,
author_prior_commits, author_failure_rate, parent_known, parent_failed, repo_recent_failure_rate
(failure rate of the repo's last 20 commits). The most important features are repo_recent_failure_rate
and parent_failed; without history, change size and is_pull_request matter most.

## How is the risk level (LOW, MEDIUM, HIGH) of a commit decided?
The risk model outputs a failure probability score for the commit, and the risk level is decided by
thresholds on that score, set on the validation set: scores below the median (p50) are LOW, scores between p50 and p85 are MEDIUM, and
the top 15% (p85 and above) are HIGH. Quantiles are used because failures are rare (8.9%), so a fixed
0.5 cut-off would label almost everything LOW. On the test set the observed failure rates were LOW 5.8%,
MEDIUM 14.5% and HIGH 22.0%. The workflow also lists risk factors, for example "source changed without
accompanying test changes".

## What is the difference between the full and diff_only risk variants?
The full variant uses all 27 features (diff + history) and needs the repository's past CI results.
The diff_only variant uses only the 19 diff features, so it works for brand-new code with no history,
such as the demo sandboxes. History is the strongest signal, so full (PR-AUC about 0.23) beats
diff_only (about 0.16).

## How is the system built? What is the architecture?
Three containers started with Docker Compose:
- ui: Streamlit dashboard on port 8502, a thin client with no pipeline code; every action is an HTTP call.
- api: FastAPI backend on port 8000 with the pipeline, risk model, self-healing loop and this assistant.
- db: PostgreSQL 16 storing workflows, events and artifacts.
Each waits for the next to be healthy (ui -> api -> db). Without Docker the API falls back to SQLite.

## How do I run the project locally?
With Docker (recommended): `docker compose up -d --build --wait`, then open http://localhost:8502 (UI)
and http://localhost:8000/docs (API). Stop with `docker compose down` (add `-v` to delete data). If
ports are busy: `API_PORT=8010 UI_PORT=8512 docker compose up -d --build --wait`.
Without Docker: `conda activate SEM7`, then `uvicorn app.api.main:app --port 8000` in one terminal and
`streamlit run ui/app.py` in another. Run the tests with `python -m pytest -q`.

## Does it have CI/CD? What do the GitHub Actions do?
CI is complete. `.github/workflows/ci.yml` runs on every push and pull request with four jobs:
1. Tests + risk-aware quality gate on Linux with a PostgreSQL service.
2. Full test suite on Windows.
3. 3-container stack: builds the images, starts ui + api + PostgreSQL, runs an end-to-end
   self-healing workflow through the API, and runs the tests inside the api image.
4. Self-healing demo: scenario 3 end to end, uploading the report and PR description.
CD is partial: deployment to AWS EC2 is scripted (`deploy/deploy.sh`) but triggered manually, not
automatically from the pipeline. Planned: an auto-deploy job after tests pass on main, a container
registry with versioned images, and rollback.

## How is the project deployed on AWS?
On one EC2 t3.small instance in ap-south-1 (Mumbai) running Ubuntu 24.04 and Docker, created with plain
AWS CLI scripts (no Terraform). `deploy/provision.sh` creates the key pair, security group (SSH only from
the admin IP, HTTP/HTTPS public), instance and Elastic IP. `deploy/deploy.sh <ip>` ships the committed
code and runs `docker compose` with the production overlay `docker-compose.prod.yml`, which adds Caddy as
a reverse proxy with automatic HTTPS (via sslip.io) and a login; ui, api and db are not exposed directly.
`deploy/teardown.sh` deletes everything. Running costs (pricing) are about US$0.50 per day.

## How do I start or stop the EC2 server?
Stop (saves money; data, IP and address are kept):
`aws ec2 stop-instances --instance-ids <instance-id> --profile sem7 --region ap-south-1`.
Start: the same command with `start-instances`; the containers start by themselves after 1-2 minutes.
Without the terminal: AWS Console, region Mumbai (ap-south-1), EC2 -> Instances -> select `sem7` ->
Instance state -> Stop or Start. While stopped only the disk and Elastic IP are billed (about US$5/month).
To delete everything permanently run `AWS_PROFILE=sem7 deploy/teardown.sh`.

## Which technologies and tools are used? (tech stack)
Python 3.11 (version used everywhere, conda env `SEM7`); Pydantic v2 for typed data contracts; pytest for real test runs; scikit-learn, pandas and
NumPy for the risk model; FastAPI and Uvicorn for the backend API; Streamlit for the dashboard;
SQLAlchemy with PostgreSQL 16 (SQLite when run without Docker) as the database; git for sandbox repos
and repair branches; Docker and Docker Compose for the three containers; GitHub Actions for CI; AWS EC2
with Caddy for deployment; Google ADK and CrewAI on Gemini for the AI repair agents (separate
`SEM7-agents` environment); and optionally the Anthropic Claude API for this assistant.

## How do I run the tests?
Project tests: `conda activate SEM7` then `python -m pytest -q` (or
`docker compose --profile tools run --rm tests` to run them inside the API image against PostgreSQL).
A full self-healing run in the terminal:
`python scripts/run_ci.py --scenario failed_first_repair --heal --approve`.
The 9 calculator tests run automatically inside every pipeline run, in the workflow's sandbox repo.

## What is stored in the database? How do I see the records?
Three tables: `workflows` (one row per run: id, scenario, state, attempt, timestamps), `events` (the
step-by-step log: state, agent, status, message, duration) and `artifacts` (saved JSON outputs: risk
assessment, pipeline run, test runs, quality gate, failure event, investigation, root cause, repair plan,
approval, repair attempt, reflection, pull request). Locally: `docker compose exec db psql -U sem7 -d sem7`
then e.g. `SELECT id, scenario, state FROM workflows;`. The Workflow History page and
`GET /workflows/{id}` show the same data without SQL.

## How do I see the logs?
Locally with Docker: `docker compose logs -f api` (or `ui`, `db`); `docker compose ps` shows container
health. On the EC2 server: `ssh ubuntu@<ip>`, `cd ~/sem7`, then
`docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f api`. Every workflow's
step-by-step log is also stored in the `events` table and shown on the Workflow History page. In GitHub
Actions, open the run on the repository's Actions tab; failed jobs print the container logs.

## What are Caddy and sslip.io used for in the deployment?
Caddy is the reverse proxy in front of the stack on EC2. It is the only container listening on the
internet (ports 80 and 443): it gets a free HTTPS certificate from Let's Encrypt automatically, asks for a
login (basic auth, user `admin`), and forwards traffic to the UI (`https://<ip-with-dashes>.sslip.io`) or
the API (`https://api.<ip-with-dashes>.sslip.io`). sslip.io is free wildcard DNS: a name like
`13-204-56-198.sslip.io` resolves to that IP, so the server gets a real domain name and certificate
without buying a domain. The config is in `deploy/Caddyfile` and `docker-compose.prod.yml`.

## What does each workflow state mean?
RECEIVED (commit received), RISK_ANALYZED (risk scored), TESTING (tests running), FAILED (CI failure
detected), INVESTIGATING, ROOT_CAUSE_IDENTIFIED, SOLUTION_PROPOSED, WAITING_APPROVAL (a fix is proposed and
waits for you; no code changed), APPROVED, REJECTED, REPAIRING (fix being applied on the repair branch),
TESTING_REPAIR (re-testing), REFLECTING (fix incomplete, analysing why), PASSED (tests passed with no
failure), PR_CREATED (fix verified and PR produced), ABORTED.

## Is the investigation done by an AI agent or an LLM? Rule-based or agents?
Both are available, chosen per run on the Run Pipeline page (⚙️ Rule-based or 🤖 AI agents), or with
`REPAIR_STRATEGY=rules|agents`. Rule-based (the default, used by CI) is deterministic and offline: it
restores operators the commit changed, then escalates to restoring the whole function from the healthy
baseline. AI agents run on Gemini: two Google ADK agents (Investigator, then Root-cause analyst, handing
off through ADK session state) and a CrewAI crew (Repair Planner, then Code Reviewer). Both plug into the
same `RepairStrategy` interface (investigate, root_cause, plan), so the safety checks, human approval,
real re-testing, reflection and PR steps are identical. A workflow keeps the mode it started with.

## How do I switch between rule-based and AI agents?
On the Run Pipeline page, step 2 "Who analyses a failure and proposes the fix?" has two options:
⚙️ Rule-based or 🤖 AI agents (Google ADK + CrewAI). Pick one before pressing Run; the workflow keeps that
mode for later attempts. Over the API: `POST /workflows` with `"strategy": "rules"` or `"agents"`, and
`GET /strategies` shows whether this backend can run agents. In the terminal:
`python scripts/run_ci.py --scenario simple_bug --heal --approve --strategy agents`. The agent option needs
the API running from the `SEM7-agents` environment (`requirements-agents.txt`) with `GOOGLE_API_KEY` and
`GEMINI_API_KEY` in `.env`; otherwise the page shows why it is unavailable. The Docker images and CI use
rules for now.

## How do the AI agents work? (ADK and CrewAI)
The agents only see an evidence package built from the real run: failing tests, error messages and
tracebacks, the developer's diff, the current and baseline code, and earlier attempts. The ADK
Investigator writes an `InvestigationReport`, the ADK Root-cause analyst reads it from shared session
state and writes a `RootCauseReport`. The CrewAI Repair Planner then writes a `PlanDraft` (which function
to replace and its complete new source); a guardrail rejects drafts that edit tests or config, are not
valid Python or target a missing function, and the planner retries with that feedback. The Code Reviewer
approves the draft or returns a corrected plan. The exact text being replaced is taken from the
repository, not typed by the model. Every agent step (time, tokens, model, retries) is written to the
workflow's event log; transient Gemini errors are retried, and if a step still fails the rule-based
strategy does that step and the log says so. Model: `gemini-3.1-flash-lite` (setting `AGENT_MODEL`). In
live tests the agents fixed scenario 1 and scenario 3 in one attempt each (rules need two for scenario 3).

## What is planned next?
Agentic AI: MCP tool servers for the agents (risk model, CI tools), A2A communication between the ADK
and CrewAI agents, more agentic patterns (hierarchical, parallel), prompt-injection and toxicity guardrails,
per-agent monitoring with a heat map and tracing, a 40-run evaluation with boxplots, and RAG retrieval.
DevOps: automatic deployment from GitHub Actions, a container registry with versioned images and
rollback, security scanning (pip-audit, Trivy, gitleaks), coverage and lint gates, monitoring with
Prometheus and Grafana or CloudWatch, database backups, a real GitHub PR adapter, and optionally
Terraform for infrastructure as code.

## How does this assistant work?
The assistant answers questions about the project and your workflow runs. It reads a curated knowledge
base (this FAQ, the README, the deployment guide and the risk model evaluation) plus live data from the
database (workflow counts, recent runs, and the full trace of a selected workflow). In offline mode
(`LLM_PROVIDER=heuristic`, the default) it finds the best-matching passages with TF-IDF search and
answers from them, quoting its sources. With `LLM_PROVIDER=anthropic` and `LLM_API_KEY` set, Claude writes
the answer grounded in the same knowledge and live data; if the API call fails it falls back to offline
mode. The assistant only reads data - it never runs pipelines, approves fixes or changes code.

## What do ROC-AUC, PR-AUC, precision and recall mean?
ROC-AUC: the probability that a random failing commit gets a higher risk score than a random passing
one (0.5 = coin flip, 1.0 = perfect). PR-AUC: average precision across thresholds; better than ROC-AUC
when failures are rare, and a blind guess scores the failure rate (about 0.12 here). Precision: of the
commits flagged risky, the share that really failed. Recall: of the commits that really failed, the
share that were flagged. F1: the harmonic mean of precision and recall.

## What is a temporal split and leave-one-repo-out?
Temporal split: each repo's commits are ordered by time; the model trains on older commits and is tested
on newer ones, like predicting the future from the past. Leave-one-repo-out: train on 8 repos and test on
the 9th, which the model has never seen, to measure whether it generalises to a new codebase.

## Why use GitHub Actions instead of Jenkins?
GitHub Actions is built into GitHub: no server to install or maintain, workflows live in the repo as
YAML, and it has hosted Linux and Windows runners plus service containers such as PostgreSQL. Jenkins is
self-hosted and needs its own server, plugins and upkeep; it is more flexible for large on-premise setups
but adds operational work this project does not need.
