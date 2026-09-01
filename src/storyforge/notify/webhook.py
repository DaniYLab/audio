"""M5-V4: webhook delivery — enqueue run_end/run_fail events and flush them.

Storage is file-based (``data/webhooks/``) so the dispatcher works without a
database; the same interface can be backed by Postgres once M5-V1 lands.
Layout::

    data/webhooks/
    ├── targets.json            # [WebhookTarget] per universe
    └── deliveries.jsonl        # append-only delivery log
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from storyforge.core.logging import get_logger

logger = get_logger(__name__)


class WebhookTarget(BaseModel):
    """One registered webhook endpoint (CLI ``webhooks add``)."""

    universe_id: str
    url: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class WebhookDelivery(BaseModel):
    """One queued/sent delivery record (persisted in deliveries.jsonl)."""

    id: str
    universe_id: str
    event: str  # "run_end" | "run_fail" | "alert"
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "delivered", "failed"] = "pending"
    attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    next_retry_at: datetime | None = None
    last_error: str | None = None


# Backoff schedule (attempts -> delay): 1m, 5m, 15m, 1h, 6h (M5 §2.4).
BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600, 21600)


@dataclass
class WebhookStore:
    """File-backed store for targets + deliveries (swap-able for Postgres)."""

    root: Path
    _targets: dict[str, list[WebhookTarget]] = field(default_factory=dict)
    _targets_loaded: bool = False

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # -- targets ----------------------------------------------------------

    def _targets_path(self) -> Path:
        return self.root / "targets.json"

    def _load_targets(self) -> None:
        if self._targets_loaded:
            return
        self._targets = {}
        path = self._targets_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for universe, entries in data.items():
                    if isinstance(entries, list):
                        self._targets[str(universe)] = [
                            WebhookTarget.model_validate(e) for e in entries if isinstance(e, dict)
                        ]
        self._targets_loaded = True

    def _save_targets(self) -> None:
        data = {u: [t.model_dump(mode="json") for t in ts] for u, ts in self._targets.items()}
        tmp = self._targets_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._targets_path())

    def list_targets(self, universe_id: str | None = None) -> list[WebhookTarget]:
        self._load_targets()
        if universe_id is None:
            return [t for ts in self._targets.values() for t in ts]
        return list(self._targets.get(universe_id, []))

    def add_target(self, target: WebhookTarget) -> None:
        self._load_targets()
        existing = self._targets.setdefault(target.universe_id, [])
        if not any(t.url == target.url for t in existing):
            existing.append(target)
        self._save_targets()

    def remove_target(self, universe_id: str, url: str) -> bool:
        self._load_targets()
        existing = self._targets.get(universe_id, [])
        kept = [t for t in existing if t.url != url]
        if len(kept) == len(existing):
            return False
        if kept:
            self._targets[universe_id] = kept
        else:
            self._targets.pop(universe_id, None)
        self._save_targets()
        return True

    # -- deliveries ---------------------------------------------------------

    def _deliveries_path(self) -> Path:
        return self.root / "deliveries.jsonl"

    def append_delivery(self, delivery: WebhookDelivery) -> None:
        line = json.dumps(delivery.model_dump(mode="json"), ensure_ascii=False)
        with self._deliveries_path().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def load_deliveries(self) -> list[WebhookDelivery]:
        path = self._deliveries_path()
        if not path.exists():
            return []
        out: list[WebhookDelivery] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(WebhookDelivery.model_validate(json.loads(line)))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    def save_deliveries(self, deliveries: list[WebhookDelivery]) -> None:
        lines = [
            json.dumps(d.model_dump(mode="json"), ensure_ascii=False) for d in deliveries
        ]
        tmp = self._deliveries_path().with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(self._deliveries_path())


class WebhookDispatcher:
    """Enqueue webhook events and flush due deliveries with backoff.

    ``http`` is injectable for tests (mock transport). ``now`` is injectable
    for deterministic retry scheduling.
    """

    def __init__(
        self,
        store: WebhookStore,
        http: httpx.Client | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._http = http
        self._now = now or (lambda: datetime.now(tz=UTC))

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))
        return self._http

    # -- public API ----------------------------------------------------------

    def enqueue(self, universe_id: str, event: str, payload: dict[str, Any]) -> int:
        """Queue a delivery for every target of ``universe_id``.

        Returns the number of deliveries enqueued (0 when no target exists).
        """
        targets = self.store.list_targets(universe_id)
        for target in targets:
            delivery = WebhookDelivery(
                id=uuid.uuid4().hex[:12],
                universe_id=universe_id,
                event=event,
                payload={**payload, "target_url": target.url},
            )
            self.store.append_delivery(delivery)
            logger.info("webhook enqueued", universe=universe_id, hook_event=event, url=target.url)
        return len(targets)

    def flush(self, max_attempts: int = 5) -> list[WebhookDelivery]:
        """Send all due (pending or retry-window-open) deliveries.

        Returns the deliveries processed this round (for tests/CLI).
        """
        now = self._now()
        deliveries = self.store.load_deliveries()
        touched: list[WebhookDelivery] = []
        changed = False
        for d in deliveries:
            if d.status == "delivered":
                continue
            if d.status == "failed" and (d.next_retry_at is None or d.next_retry_at > now):
                continue  # not due yet
            if d.attempts >= max_attempts:
                continue  # give up
            ok, error = self._send(d)
            d.attempts += 1
            if ok:
                d.status = "delivered"
                d.next_retry_at = None
                d.last_error = None
            else:
                d.status = "failed"
                d.last_error = error
                delay = BACKOFF_SECONDS[min(d.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                d.next_retry_at = self._shift(now, delay)
            touched.append(d)
            changed = True
        if changed:
            self.store.save_deliveries(deliveries)
        return touched

    def _send(self, delivery: WebhookDelivery) -> tuple[bool, str | None]:
        url = str(delivery.payload.get("target_url") or "")
        if not url:
            return False, "no target_url in payload"
        try:
            resp = self.http.post(
                url,
                json={
                    "event": delivery.event,
                    "universe_id": delivery.universe_id,
                    "payload": delivery.payload,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return False, f"transport error: {exc}"
        if resp.status_code in (200, 201, 204):
            return True, None
        return False, f"http {resp.status_code}: {resp.text[:200]}"

    def _shift(self, now: datetime, seconds: int) -> datetime:
        return now + timedelta(seconds=seconds)


def build_dispatcher(root: Path | None = None) -> WebhookDispatcher:
    """Build a dispatcher over the default data dir (for CLI/tests)."""
    return WebhookDispatcher(WebhookStore(root or Path("data/webhooks")))


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()
