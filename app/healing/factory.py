"""Pick the repair strategy from settings: rule-based (default) or the ADK + CrewAI agents."""

from __future__ import annotations

from app.config import settings
from app.healing.strategy import RepairStrategy


def make_strategy(name: str | None = None) -> RepairStrategy:
    name = (name or settings.repair_strategy).lower()
    if name in ("rules", "rule-based"):
        from app.healing.rules import RuleBasedStrategy

        return RuleBasedStrategy()
    if name in ("agents", "agentic", "adk+crewai"):
        try:
            from app.agents.strategy import AgenticStrategy
        except ImportError as exc:  # frameworks live in requirements-agents.txt, not the base image
            raise RuntimeError(f"REPAIR_STRATEGY=agents needs the agent packages "
                               f"(pip install -r requirements-agents.txt): {exc}") from exc
        return AgenticStrategy()
    raise ValueError(f"unknown REPAIR_STRATEGY {name!r} (use 'rules' or 'agents')")


STRATEGY_LABELS = {"rules": "Rule-based", "agents": "AI agents (Google ADK + CrewAI)"}


def strategy_options() -> list[dict]:
    """Which strategies this backend can run right now, and why not when it can't."""
    import importlib.util

    def installed(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:  # parent package (e.g. `google`) missing
            return False

    missing = [m for m in ("google.adk", "crewai") if not installed(m)]
    has_key = bool(settings.google_api_key or settings.gemini_api_key)
    reason = (f"agent packages not installed in this backend ({', '.join(missing)}) - "
              "run the API from the SEM7-agents environment (requirements-agents.txt)" if missing
              else "no Gemini key - set GOOGLE_API_KEY and GEMINI_API_KEY in .env" if not has_key else "")
    return [
        {"id": "rules", "label": STRATEGY_LABELS["rules"], "available": True, "reason": "",
         "default": settings.repair_strategy.lower() in ("rules", "rule-based")},
        {"id": "agents", "label": STRATEGY_LABELS["agents"], "available": not reason, "reason": reason,
         "model": settings.agent_model, "default": settings.repair_strategy.lower() == "agents"},
    ]
