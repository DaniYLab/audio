"""Retry policy for external services.

Rules:
- Only retries errors marked ``retryable=True`` (see ExternalServiceError);
  deterministic failures (auth, bad request, quota) fail fast.
- Exponential backoff with jitter; budget is capped so a hung provider can
  never stall a pipeline run indefinitely.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from storyforge.core.exceptions import ExternalServiceError

T = TypeVar("T")

_MAX_ATTEMPTS = 4
_WAIT_MIN_SECONDS = 1.0
_WAIT_MAX_SECONDS = 30.0


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, ExternalServiceError) and exc.retryable


def retry_external(call: Callable[[], T]) -> T:
    """Run ``call`` with the standard external-service retry policy."""
    retryer = Retrying(
        retry=retry_if_exception(_is_retryable),
        wait=wait_random_exponential(multiplier=1, min=_WAIT_MIN_SECONDS, max=_WAIT_MAX_SECONDS),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        reraise=True,
    )
    return retryer(call)
