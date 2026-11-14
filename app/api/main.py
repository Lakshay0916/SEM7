"""SEM7 backend API (FastAPI).

The UI is a thin client over these endpoints; all pipeline, risk and
self-healing logic lives here. Run locally with:

    uvicorn app.api.main:app --port 8000
"""

from __future__ import annotations

import json
from pathlib import Path
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.assistant.bot import Assistant
from app.assistant.knowledge import KnowledgeBase
from app.ci.scenarios import SCENARIOS
from app.config import PROJECT_ROOT, settings
from app.git.repo import GitRepo
from app.git.safety import try_ai_commit_to_main
from app.healing.loop import HealingError, abort, apply_and_verify, decide, propose_fix
from app.healing.factory import make_strategy, strategy_options
from app.healing.strategy import RepairStrategy
from app.orchestration.pipeline import run_ci_workflow
from app.orchestration.state import InvalidTransition
from app.orchestration.store import Store
from app.risk.predictor import MODEL_PATH, RiskPredictor
from app.observability import otel
from app.observability.monitoring import monitoring
from app.observability.tracing import to_otlp
from app.security.input_guard import check_text
from app.security.personas import run_personas

RISK_DIR = PROJECT_ROOT / "data" / "risk"
EVAL_DIR = PROJECT_ROOT / "data" / "eval"

app = FastAPI(
    title="SEM7 Self-Healing CI/CD API",
    version="1.0.0",
    description="Risk-aware CI pipeline with a human-approved self-healing loop.",
)
otel.setup("sem7-api", app=app)  # OpenTelemetry traces / metrics / logs, when OTEL_EXPORTER_OTLP_ENDPOINT is set


# --------------------------------------------------------------------------- dependencies


@lru_cache
def get_store() -> Store:
    return Store()


@lru_cache
def get_predictor() -> RiskPredictor | None:
    return RiskPredictor.load() if MODEL_PATH.exists() else None


def get_strategy(name: str | None = None, messaging: str | None = None,
                 pattern: str | None = None) -> RepairStrategy:
    return make_strategy(name, messaging, pattern)


def strategy_for(store: Store, wf_id: str) -> RepairStrategy:
    """The strategy a workflow was started with, so retries after reflection use the same one."""
    choice = store.latest(wf_id, "strategy_choice") or {}
    return get_strategy(choice.get("strategy"), choice.get("messaging"), choice.get("pattern"))


@lru_cache
def get_knowledge() -> KnowledgeBase:
    return KnowledgeBase.load()


def get_assistant() -> Assistant:
    return Assistant(get_store(), get_predictor(), kb=get_knowledge())


@app.exception_handler(KeyError)
def _not_found(_, exc: KeyError):
    return JSONResponse(status_code=404, content={"detail": str(exc).strip("'")})


@app.exception_handler(HealingError)
@app.exception_handler(InvalidTransition)
def _conflict(_, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# --------------------------------------------------------------------------- schemas


class RunRequest(BaseModel):
    scenario: str = Field(examples=["failed_first_repair"])
    heal: bool = Field(default=True, description="On failure, investigate and propose a fix (no code changes)")
    strategy: Literal["rules", "agents"] | None = Field(
        default=None, description="Who analyses failures: rule-based or ADK + CrewAI agents (default: server setting)")
    messaging: Literal["blackboard", "direct"] | None = Field(
        default=None, description="Agents only: share context via a blackboard or direct hand-off (default: server)")
    pattern: Literal["sequential", "parallel", "hierarchical"] | None = Field(
        default=None, description="Agents only: one after another, parallel investigators, or a lead agent that "
                                  "delegates (default: server setting)")


class Decision(BaseModel):
    reviewer: str = Field(default="reviewer", min_length=1, max_length=80)
    comment: str = Field(default="", max_length=500)


class AbortRequest(BaseModel):
    reason: str = Field(default="Stopped by reviewer", max_length=500)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, examples=["How does the quality gate decide?"])
    history: list[ChatTurn] = Field(default_factory=list, max_length=40,
                                    description="Earlier turns of this conversation, oldest first")
    workflow_id: str | None = Field(default=None, description="Optional workflow to ask about")


def _summary(store: Store, wf_id: str) -> dict:
    wf = store.get_workflow(wf_id)
    return {"workflow_id": wf_id, "state": wf["state"], "attempt": wf["attempt"]}


# --------------------------------------------------------------------------- health & metadata


@app.get("/health", tags=["meta"])
def health() -> dict:
    store = get_store()
    try:
        db_ok = store.ping()
    except Exception as exc:  # report, don't crash: lets orchestrators see the DB is the problem
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}") from None
    return {"status": "ok", "database": store.backend, "db_ok": db_ok,
            "risk_model": get_predictor().version if get_predictor() else None}


@app.get("/strategies", tags=["meta"])
def strategies() -> list[dict]:
    """Repair strategies (rule-based / AI agents) and whether this backend can run them."""
    return strategy_options()


@app.get("/scenarios", tags=["meta"])
def scenarios() -> list[dict]:
    return [
        {"id": s.id, "title": s.title, "description": s.description, "branch": s.branch,
         "commit_message": s.commit_message,
         "edits": [{"file": e.file, "old": e.old, "new": e.new} for e in s.edits]}
        for s in SCENARIOS.values()
    ]


# --------------------------------------------------------------------------- workflows


@app.post("/workflows", status_code=201, tags=["workflows"])
def run_workflow(req: RunRequest) -> dict:
    if req.scenario not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {req.scenario!r}")
    name = req.strategy or ("agents" if settings.repair_strategy.lower() == "agents" else "rules")
    option = next(o for o in strategy_options() if o["id"] == name)
    if not option["available"]:
        raise HTTPException(status_code=409, detail=f"{option['label']} unavailable: {option['reason']}")
    store = get_store()
    messaging = (req.messaging or settings.agent_messaging) if name == "agents" else None
    pattern = (req.pattern or settings.agent_pattern) if name == "agents" else None
    result = run_ci_workflow(req.scenario, store, get_predictor(),
                             strategy=get_strategy(name, messaging, pattern) if req.heal else None)
    store.save_artifact(result.workflow_id, "strategy_choice",
                        {"strategy": name, "messaging": messaging, "pattern": pattern})
    return {**_summary(store, result.workflow_id), "strategy": name, "messaging": messaging, "pattern": pattern}


@app.get("/workflows", tags=["workflows"])
def list_workflows(limit: int = 100) -> list[dict]:
    return get_store().list_workflows(limit=min(max(limit, 1), 500))


@app.get("/workflows/{wf_id}", tags=["workflows"])
def get_workflow(wf_id: str) -> dict:
    """Full trace: workflow row, every event, every artifact."""
    return get_store().trace(wf_id)


@app.get("/workflows/{wf_id}/repository", tags=["workflows"])
def workflow_repository(wf_id: str) -> dict:
    """The workflow's sandbox git repository, read-only: branches, the history of all branches (the developer's
    commit and every AI repair commit, each on its own branch) and the pull-request file."""
    repo = Path(get_store().get_workflow(wf_id)["repo_path"]).resolve()
    # only ever a sandbox the pipeline created: <workspace>/runs/<run id>/repo
    if repo.name != "repo" or repo.parent.parent.name != "runs" or not (repo / ".git").exists():
        raise HTTPException(status_code=404, detail="this workflow's sandbox repository is no longer on this server")
    pr = next(iter(sorted(repo.parent.glob("PR_*.md"))), None)
    return {"workflow_id": wf_id, "path": str(repo), **GitRepo(repo).overview(),
            "pull_request": {"file": pr.name, "content": pr.read_text(encoding="utf-8")[:20000]} if pr else None}


@app.get("/workflows/{wf_id}/trace", tags=["observability"])
def get_trace(wf_id: str, attempt: int | None = None, format: Literal["spans", "otlp"] = "spans") -> dict:
    """The traces of a workflow's agent attempts: spans (agent, LLM, tool, A2A, guardrail), or OTLP JSON."""
    store = get_store()
    store.get_workflow(wf_id)  # 404 for an unknown workflow
    traces = [{"attempt": a["attempt"], **a["payload"]} for a in store.artifacts(wf_id, "trace")
              if attempt is None or a["attempt"] == attempt]
    if format == "otlp":
        return to_otlp([s for t in traces for s in t["spans"]])
    return {"workflow_id": wf_id, "traces": traces}


@app.get("/monitoring/agents", tags=["observability"])
def agent_monitoring(limit: int = 300) -> dict:
    """Per-agent time, LLM calls, tokens, tool calls and errors from every saved trace (heat map data)."""
    return monitoring(get_store(), limit=limit)


@app.post("/workflows/{wf_id}/propose", tags=["self-healing"])
def propose(wf_id: str) -> dict:
    store = get_store()
    propose_fix(store, wf_id, strategy_for(store, wf_id))
    return _summary(store, wf_id)


@app.post("/workflows/{wf_id}/approve", tags=["self-healing"])
def approve(wf_id: str, d: Decision) -> dict:
    """Record the human approval, apply on the repair branch, re-test, then PR or reflect."""
    store = get_store()
    decide(store, wf_id, True, d.reviewer, d.comment)
    apply_and_verify(store, wf_id, strategy=strategy_for(store, wf_id))
    return _summary(store, wf_id)


@app.post("/workflows/{wf_id}/reject", tags=["self-healing"])
def reject(wf_id: str, d: Decision) -> dict:
    store = get_store()
    decide(store, wf_id, False, d.reviewer, d.comment)
    return _summary(store, wf_id)


@app.post("/workflows/{wf_id}/abort", tags=["self-healing"])
def abort_workflow(wf_id: str, req: AbortRequest) -> dict:
    store = get_store()
    abort(store, wf_id, req.reason)
    return _summary(store, wf_id)


@app.post("/workflows/{wf_id}/safety-check", tags=["safety"])
def safety_check(wf_id: str) -> dict:
    """Try an AI-actor commit on main in this workflow's sandbox; the guard must block it."""
    return try_ai_commit_to_main(get_store().get_workflow(wf_id)["repo_path"])


# --------------------------------------------------------------------------- risk model


@app.get("/risk/model", tags=["risk"])
def risk_model() -> dict:
    p = get_predictor()
    if p is None:
        raise HTTPException(status_code=404, detail="risk model not trained")
    return {"version": p.version,
            "variants": {name: {"chosen_model": v["chosen_model"], "risk_levels": v["risk_levels"]}
                         for name, v in p.bundle["variants"].items()}}


@app.get("/risk/evaluation", tags=["risk"])
def risk_evaluation() -> dict:
    def load(name: str):
        path = RISK_DIR / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    md = RISK_DIR / "EVALUATION.md"
    caveats = None
    if md.exists() and "## Caveats" in (text := md.read_text(encoding="utf-8")):
        caveats = text.split("## Caveats", 1)[1]
    return {"evaluation": load("evaluation.json"), "mining_summary": load("mining_summary.json"),
            "caveats": caveats}


@app.get("/evaluation", tags=["evaluation"])
def agent_evaluation() -> dict:
    """The agent evaluation (scripts/run_evaluation.py): every run's measurements and the summary."""
    runs_file = EVAL_DIR / "runs.jsonl"
    runs = [json.loads(line) for line in runs_file.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if runs_file.exists() else []
    summary = EVAL_DIR / "summary.json"
    return {"runs": runs, "summary": json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else None}


# --------------------------------------------------------------------------- notifications


@app.post("/notifications/test", tags=["notifications"])
def notification_test() -> dict:
    """Send a test message through the configured channel (Telegram) to check the setup."""
    from app.notifications.notifier import channel

    via = channel()
    if via is None:
        return {"configured": False, "sent": False,
                "detail": "set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (and NOTIFY is not 0)"}
    try:
        via.send("🔔 <b>SEM7</b> test message: notifications are set up. You will hear about fixes waiting for "
                 "approval, pull requests created and workflows that stop.")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        for secret in settings.secret_values():
            detail = detail.replace(secret, "[REDACTED]")
        return {"configured": True, "sent": False, "detail": detail}
    return {"configured": True, "sent": True, "detail": "test message sent"}


# --------------------------------------------------------------------------- guardrails


class GuardrailCheck(BaseModel):
    text: str = Field(min_length=1, max_length=20000, examples=["Ignore all previous instructions"])
    context: Literal["chat", "evidence"] = Field(
        default="chat", description="chat = a user message; evidence = repository content the agents will read")


@app.post("/guardrails/check", tags=["guardrails"])
def guardrail_check(req: GuardrailCheck) -> dict:
    """Run the input guardrail on any text: allow / flag / block, with every finding and its rule."""
    return check_text(req.text, req.context).to_dict()


@app.get("/guardrails/personas", tags=["guardrails"])
def guardrail_personas() -> dict:
    """Run every test persona's prompts through the input guardrail (deterministic, no LLM)."""
    return run_personas()


# --------------------------------------------------------------------------- assistant


@app.get("/assistant", tags=["assistant"])
def assistant_info() -> dict:
    """Which answer mode is active (offline heuristic or Gemini) and suggested questions."""
    return get_assistant().info()


@app.post("/chat", tags=["assistant"])
def chat(req: ChatRequest) -> dict:
    """Answer a question about the project or a workflow run. Read-only: never changes anything."""
    history = [t.model_dump() for t in req.history]
    return get_assistant().reply(req.message, history, req.workflow_id).to_dict()
