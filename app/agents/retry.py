"""Retry transient LLM errors (overloaded model, rate limit) with backoff, recording each retry."""

from __future__ import annotations

import re
import time
from typing import Callable, TypeVar

from app.agents.models import AgentRecord

T = TypeVar("T")
TRANSIENT = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "high demand", "rate limit")


RETRY_AFTER = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s|retryDelay['\"]?:\s*['\"](\d+(?:\.\d+)?)s", re.I)
MAX_WAIT = 90.0


def retry_after(exc: BaseException) -> float | None:
    """Seconds the API asked us to wait (Gemini's 429 says e.g. "Please retry in 22.3s"), if it said."""
    m = RETRY_AFTER.search(str(exc))
    return min(float(m.group(1) or m.group(2)), MAX_WAIT) if m else None


def is_transient(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return any(marker.lower() in text.lower() for marker in TRANSIENT)


def with_retries(fn: Callable[[], T], agent: str, framework: str, records: list[AgentRecord],
                 attempts: int = 4, base_delay: float = 3.0, sleep: Callable[[float], None] = time.sleep) -> T:
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1 or not is_transient(exc):
                raise
            delay = max(base_delay * (2 ** i), (retry_after(exc) or 0) + 1)  # honour the API's own wait
            records.append(AgentRecord(agent=agent, framework=framework, status="retry",
                                       message=f"transient error ({type(exc).__name__}); retrying in {delay:.0f}s"))
            sleep(delay)
    raise AssertionError("unreachable")
