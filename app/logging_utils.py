"""Logging setup with secret redaction and per-stage timing."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from app.config import settings


class SecretRedactingFilter(logging.Filter):
    """Replaces any configured secret value appearing in a log record."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets if secrets is not None else settings.secret_values()) if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            for s in self.secrets:
                msg = msg.replace(s, "[REDACTED]")
            record.msg, record.args = msg, None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
    handler.addFilter(SecretRedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


@contextmanager
def stage(logger: logging.Logger, agent: str, what: str) -> Iterator[dict]:
    """Log start/finish/error of a pipeline stage with its duration.

    Yields a dict whose 'duration_ms' is filled in on exit.
    """
    info: dict = {"duration_ms": None}
    logger.info("[%s] %s started", agent, what)
    t0 = time.monotonic()
    try:
        yield info
    except Exception as exc:
        info["duration_ms"] = int((time.monotonic() - t0) * 1000)
        logger.error("[%s] %s failed after %dms: %s", agent, what, info["duration_ms"], exc)
        raise
    info["duration_ms"] = int((time.monotonic() - t0) * 1000)
    logger.info("[%s] %s finished in %dms", agent, what, info["duration_ms"])
