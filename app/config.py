"""Central configuration, loaded from environment variables (and .env if present).

Secrets (LLM_API_KEY, GITHUB_TOKEN) are only ever read from the environment and
are excluded from repr() so they cannot leak into logs by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name) or default)
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass(frozen=True)
class Settings:
    workspace_dir: Path = field(default_factory=lambda: _path("WORKSPACE_DIR", "workspace"))
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL") or "sqlite:///workspace/sem7.sqlite3"
    )
    test_timeout_seconds: int = field(default_factory=lambda: _int("TEST_TIMEOUT_SECONDS", 60))
    max_repair_attempts: int = field(default_factory=lambda: _int("MAX_REPAIR_ATTEMPTS", 3))
    protected_branches: tuple[str, ...] = ("main", "master")
    repair_branch_prefix: str = "ai/repair/"

    # Project assistant: "heuristic" (offline, default) or "gemini" (LLM answers with the agents' key and model)
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER") or "heuristic")
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL") or "")  # empty = AGENT_MODEL
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""), repr=False)
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""), repr=False)

    # Self-healing analysis: "rules" (deterministic, default - CI uses this) or "agents" (ADK + CrewAI on Gemini)
    repair_strategy: str = field(default_factory=lambda: os.getenv("REPAIR_STRATEGY") or "rules")
    agent_model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL") or "gemini-3.1-flash-lite")
    # How the agents share context: "blackboard" (shared state, default) or "direct" (point-to-point hand-off)
    agent_messaging: str = field(default_factory=lambda: os.getenv("AGENT_MESSAGING") or "blackboard")
    # How the agents are organised: "sequential" (one after another, default), "parallel" (three specialist
    # investigators at once, ADK ParallelAgent) or "hierarchical" (a lead agent delegates, CrewAI hierarchical)
    agent_pattern: str = field(default_factory=lambda: os.getenv("AGENT_PATTERN") or "sequential")
    # Where the agents run: "local" (in this process) or "a2a" (separate agent services, called over A2A)
    agent_transport: str = field(default_factory=lambda: os.getenv("AGENT_TRANSPORT") or "local")
    adk_agent_url: str = field(default_factory=lambda: os.getenv("ADK_AGENT_URL") or "http://localhost:8101/")
    crew_agent_url: str = field(default_factory=lambda: os.getenv("CREW_AGENT_URL") or "http://localhost:8102/")
    agent_tools: bool = field(default_factory=lambda: os.getenv("AGENT_TOOLS", "1") not in ("0", "false", "no"))
    agent_fallback: bool = field(default_factory=lambda: os.getenv("AGENT_FALLBACK", "1") not in ("0", "false", "no"))
    google_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_API_KEY", ""), repr=False)
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""), repr=False)
    repository_url: str = field(default_factory=lambda: os.getenv("REPOSITORY_URL", ""))
    # Telegram notifications (fix waiting for approval / PR created / stopped). Off without a token and chat id;
    # NOTIFY=0 switches them off everywhere (the evaluation runner and the tests do).
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""), repr=False)
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    notify: bool = field(default_factory=lambda: os.getenv("NOTIFY", "1") not in ("0", "false", "no"))
    # Public address of the web UI, for links in notifications (on EC2: https://<SITE_HOST>)
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL")
                            or (f"https://{os.environ['SITE_HOST']}" if os.getenv("SITE_HOST") else ""))

    def secret_values(self) -> list[str]:
        """Non-empty secret values, used by the log redaction filter."""
        return list(dict.fromkeys(s for s in (self.llm_api_key, self.github_token, self.google_api_key,
                                              self.gemini_api_key, self.telegram_bot_token) if s))


settings = Settings()
