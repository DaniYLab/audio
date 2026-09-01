"""Structured logging via structlog.

Rules:
- Modules never configure logging; only the CLI entrypoint calls
  ``configure_logging`` once. Libraries use ``get_logger(__name__)``.
- Logs are structured key/value; never log secrets (redact at call sites —
  SecretStr fields in settings must never be passed to log calls).
- M3-W6: every run also appends JSON lines to ``data/logs/runs/<date>.jsonl``
  (size-rotated 10 × 10 MB) for centralized/ops observability.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
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

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=(
            structlog.dev.ConsoleRenderer()
            if settings.log_format == "console"
            else structlog.processors.JSONRenderer()
        )
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    # M3-W6: structured file sink — JSON lines, size-rotated.
    log_dir = Path("data/logs/runs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer()
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / f"{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}.jsonl",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    root = logging.getLogger()
    root.handlers = [console_handler, file_handler]
    root.setLevel(level)

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_run_context(**kwargs: Any) -> None:
    """Attach run-wide context (project id, run id) to every log line."""
    structlog.contextvars.bind_contextvars(**kwargs)
