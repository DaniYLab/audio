"""Structured logging via structlog.

Rules:
- Modules never configure logging; only the CLI entrypoint calls
  ``configure_logging`` once. Libraries use ``get_logger(__name__)``.
- Logs are structured key/value; never log secrets (redact at call sites —
  SecretStr fields in settings must never be passed to log calls).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from storyforge.core.config import Settings

_CONFIGURED = False


def configure_logging(settings: Settings) -> None:
    """Idempotent one-time logging setup. Call only from the CLI."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if settings.log_format == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_run_context(**kwargs: Any) -> None:
    """Attach run-wide context (project id, run id) to every log line."""
    structlog.contextvars.bind_contextvars(**kwargs)
