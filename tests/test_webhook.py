"""M5-V4: webhook dispatcher unit tests — mock httpx transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from storyforge.notify.webhook import (
    BACKOFF_SECONDS,
    WebhookDispatcher,
    WebhookStore,
    WebhookTarget,
)


@pytest.fixture()
def store(tmp_path: Path) -> WebhookStore:
    return WebhookStore(tmp_path / "webhooks")


def _ok() -> httpx.Client:
    class _Ok(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request)

    return httpx.Client(transport=_Ok())


def _fail_500() -> httpx.Client:
    class _Fail(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error", request=request)

    return httpx.Client(transport=_Fail())


def _timeout() -> httpx.Client:
    class _Timeout(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

    return httpx.Client(transport=_Timeout())


# -- target management -------------------------------------------------------


def test_add_target(store: WebhookStore) -> None:
    target = WebhookTarget(universe_id="u1", url="https://example.com/hook")
    store.add_target(target)
    assert store.list_targets("u1") == [target]


def test_list_targets_all(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    store.add_target(WebhookTarget(universe_id="u2", url="https://b.com/hook"))
    assert len(store.list_targets()) == 2


def test_remove_target(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    assert store.remove_target("u1", "https://a.com/hook") is True
    assert store.list_targets("u1") == []


def test_remove_nonexistent(store: WebhookStore) -> None:
    assert store.remove_target("u1", "https://nope.com/hook") is False


def test_add_duplicate_url(store: WebhookStore) -> None:
    t = WebhookTarget(universe_id="u1", url="https://a.com/hook")
    store.add_target(t)
    store.add_target(t)
    assert len(store.list_targets("u1")) == 1


# -- enqueue -----------------------------------------------------------------


def test_enqueue_creates_deliveries(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    dispatcher = WebhookDispatcher(store, http=_ok())
    count = dispatcher.enqueue("u1", "run_end", {"project": "p1"})
    assert count == 1
    deliveries = store.load_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].event == "run_end"
    assert deliveries[0].status == "pending"


def test_enqueue_no_target(store: WebhookStore) -> None:
    dispatcher = WebhookDispatcher(store, http=_ok())
    count = dispatcher.enqueue("u1", "run_end", {})
    assert count == 0


# -- flush -------------------------------------------------------------------


def test_flush_delivers_ok(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    dispatcher = WebhookDispatcher(store, http=_ok())
    dispatcher.enqueue("u1", "run_end", {"project": "p1"})
    processed = dispatcher.flush()
    assert len(processed) == 1
    assert processed[0].status == "delivered"
    assert processed[0].attempts == 1


def test_flush_failed_retries_with_backoff(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    now = datetime.now(tz=UTC)
    dispatcher = WebhookDispatcher(store, http=_fail_500(), now=lambda: now)
    dispatcher.enqueue("u1", "run_fail", {"project": "p1"})
    processed = dispatcher.flush()
    assert len(processed) == 1
    assert processed[0].status == "failed"
    assert processed[0].attempts == 1
    # next_retry_at should be now + 60s (first backoff slot).
    assert processed[0].next_retry_at is not None
    expected = now + timedelta(seconds=BACKOFF_SECONDS[0])
    assert abs((processed[0].next_retry_at - expected).total_seconds()) < 1


def test_flush_retry_after_backoff(store: WebhookStore) -> None:
    """After backoff expires, flush retries the delivery."""
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    now = datetime.now(tz=UTC)
    # First attempt: fails.
    dispatcher = WebhookDispatcher(store, http=_fail_500(), now=lambda: now)
    dispatcher.enqueue("u1", "run_fail", {"project": "p1"})
    dispatcher.flush()
    # Second attempt: time moves forward past backoff, now with OK mock.
    later = now + timedelta(seconds=BACKOFF_SECONDS[0] + 1)
    dispatcher2 = WebhookDispatcher(store, http=_ok(), now=lambda: later)
    processed = dispatcher2.flush()
    assert len(processed) == 1
    assert processed[0].status == "delivered"
    assert processed[0].attempts == 2


def test_flush_skips_not_due(store: WebhookStore) -> None:
    """Delivery whose next_retry_at is in the future is skipped."""
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    now = datetime.now(tz=UTC)
    dispatcher = WebhookDispatcher(store, http=_fail_500(), now=lambda: now)
    dispatcher.enqueue("u1", "run_fail", {"project": "p1"})
    dispatcher.flush()  # fails, schedules retry at now+60s
    # Still at now — no retry yet.
    processed = dispatcher.flush()
    assert len(processed) == 0


def test_flush_gives_up_after_max_attempts(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    now = datetime.now(tz=UTC)
    dispatcher = WebhookDispatcher(store, http=_fail_500(), now=lambda: now)
    dispatcher.enqueue("u1", "run_fail", {"project": "p1"})
    # Flush 5 times, each time advancing time past the backoff window.
    for attempt in range(5):
        now = now + timedelta(seconds=BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)] + 1)
        dispatcher = WebhookDispatcher(
            store, http=_fail_500(), now=lambda n=now: n
        )
        dispatcher.flush()
    # 6th attempt: should give up (max_attempts=5).
    now = now + timedelta(hours=24)
    dispatcher = WebhookDispatcher(store, http=_ok(), now=lambda n=now: n)
    processed = dispatcher.flush()
    assert len(processed) == 0


def test_flush_timeout_error(store: WebhookStore) -> None:
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    dispatcher = WebhookDispatcher(store, http=_timeout())
    dispatcher.enqueue("u1", "run_end", {"project": "p1"})
    processed = dispatcher.flush()
    assert len(processed) == 1
    assert processed[0].status == "failed"
    assert "transport error" in (processed[0].last_error or "")


# -- delivery persistence ----------------------------------------------------


def test_deliveries_persist_across_reload(tmp_path: Path) -> None:
    store = WebhookStore(tmp_path / "webhooks")
    store.add_target(WebhookTarget(universe_id="u1", url="https://a.com/hook"))
    dispatcher = WebhookDispatcher(store, http=_ok())
    dispatcher.enqueue("u1", "run_end", {"project": "p1"})
    dispatcher.flush()
    # Reload from disk.
    store2 = WebhookStore(tmp_path / "webhooks")
    deliveries = store2.load_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].status == "delivered"
