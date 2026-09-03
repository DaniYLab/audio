"""Unit tests for the M4-A6 multi-worker queue (filesystem-backed)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storyforge.queue import JobSpec

import pytest

from storyforge.core.config import QueueSettings, Settings
from storyforge.queue import Lease, QueueManager, new_job


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm__api_key="test-key",
        queue=QueueSettings(
            queue_dir=tmp_path / "queue",
            lease_ttl_seconds=300,  # long enough for tests
            heartbeat_interval_seconds=60,
            worker_id="test_worker",
            scheduled_grace_seconds=0,
        ),
    )


@pytest.fixture()
def manager(settings: Settings) -> QueueManager:
    return QueueManager(settings)


@pytest.fixture()
def job(settings: Settings) -> JobSpec:
    j = new_job("test_proj", universe="test_universe")
    # Write the job into the queue dir so it's claimable.
    j.save(settings.queue.queue_dir / f"{j.id}.yaml")
    return j


# -- claim -------------------------------------------------------------------


def test_claim_returns_job(manager: QueueManager, job: JobSpec) -> None:
    claimed = manager.claim()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.project == job.project


def test_claim_moves_file_to_processing(manager: QueueManager, job: JobSpec) -> None:
    manager.claim()
    # Source file gone.
    assert not (manager.queue_dir / f"{job.id}.yaml").exists()
    # Processing file present.
    assert (manager.processing_dir / f"{job.id}.yaml").exists()


def test_claim_writes_lease(manager: QueueManager, job: JobSpec) -> None:
    manager.claim()
    lease_path = manager.leases_dir / f"{job.id}.lease"
    assert lease_path.exists()
    lease = Lease.from_dict(json.loads(lease_path.read_text(encoding="utf-8")))
    assert lease.worker_id == "test_worker"
    assert lease.expires_at > time.time()  # not expired immediately


def test_claim_idempotent_two_workers(
    manager: QueueManager, settings: Settings, job: JobSpec
) -> None:
    """Second claim on the same queue returns None."""
    manager.claim()
    manager2 = QueueManager(settings)
    assert manager2.claim() is None


def test_claim_empty_queue_returns_none(manager: QueueManager) -> None:
    assert manager.claim() is None


def test_claim_skips_scheduled_not_due(
    manager: QueueManager, settings: Settings, job: JobSpec
) -> None:
    """A job with far-future scheduled_at is skipped."""
    job.scheduled_at = time.time() + 3600  # 1 hour from now
    job.save(settings.queue.queue_dir / f"{job.id}.yaml")
    assert manager.claim() is None


def test_claim_respects_scheduled_grace(
    settings: Settings, job: JobSpec
) -> None:
    """A job slightly past due time with grace is claimable."""
    settings.queue.scheduled_grace_seconds = 60
    settings.queue.lease_ttl_seconds = 300
    settings.queue.heartbeat_interval_seconds = 60
    manager = QueueManager(settings)
    job.scheduled_at = time.time() + 30  # 30s in future, within 60s grace
    job.save(settings.queue.queue_dir / f"{job.id}.yaml")
    assert manager.claim() is not None


# -- lease / heartbeat / reclaim --------------------------------------------


def test_heartbeat_renews_lease(manager: QueueManager, job: JobSpec) -> None:
    manager.claim()
    orig = Lease.from_dict(
        json.loads((manager.leases_dir / f"{job.id}.lease").read_text(encoding="utf-8"))
    )
    time.sleep(0.01)  # ensure clock ticks
    manager.heartbeat_for(job.id)
    renewed = Lease.from_dict(
        json.loads((manager.leases_dir / f"{job.id}.lease").read_text(encoding="utf-8"))
    )
    assert renewed.expires_at > orig.expires_at


def test_reclaim_expired_lease_reverts_to_queue(
    manager: QueueManager, settings: Settings, job: JobSpec
) -> None:
    """A processing job with an expired lease is moved back to the queue."""
    manager.claim()
    # Overwrite lease with an expired one.
    expired = Lease("stale_worker", 999999, 1)
    expired.expires_at = time.time() - 100
    manager._write_lease(job.id, expired)
    assert manager.reclaim_expired() == 1
    # Job is back in the queue.
    assert (manager.queue_dir / f"{job.id}.yaml").exists()
    # Lease file removed.
    assert not (manager.leases_dir / f"{job.id}.lease").exists()


def test_reclaim_skips_own_active_lease(
    manager: QueueManager, job: JobSpec
) -> None:
    """A processing job with a valid lease from the same worker is not reclaimed."""
    manager.claim()
    manager.heartbeat_for(job.id)  # fresh lease
    assert manager.reclaim_expired() == 0


def test_reclaim_skips_no_lease_file(
    manager: QueueManager, job: JobSpec
) -> None:
    """A processing job with no lease file is skipped (not our format)."""
    manager.claim()
    (manager.leases_dir / f"{job.id}.lease").unlink()
    assert manager.reclaim_expired() == 0


# -- finish ------------------------------------------------------------------


def test_finish_ok(manager: QueueManager, job: JobSpec) -> None:
    manager.claim()
    manager.finish(job, ok=True)
    assert (manager.done_dir / f"{job.id}.yaml").exists()
    assert not (manager.processing_dir / f"{job.id}.yaml").exists()
    # Lease cleaned up.
    assert not (manager.leases_dir / f"{job.id}.lease").exists()


def test_finish_failed_with_error(manager: QueueManager, job: JobSpec) -> None:
    manager.claim()
    manager.finish(job, ok=False, error="something went wrong")
    assert (manager.failed_dir / f"{job.id}.yaml").exists()
    assert (manager.failed_dir / f"{job.id}.error.txt").exists()
    assert "something went wrong" in (manager.failed_dir / f"{job.id}.error.txt").read_text(
        encoding="utf-8"
    )


# -- cancel / status ---------------------------------------------------------


def test_cancel_queued_job(manager: QueueManager, job: JobSpec) -> None:
    assert manager.cancel(job.id) is True
    assert not (manager.queue_dir / f"{job.id}.yaml").exists()


def test_cancel_unknown_job(manager: QueueManager) -> None:
    assert manager.cancel("no_such_job") is False


def test_status_counts(manager: QueueManager, job: JobSpec) -> None:
    assert manager.status() == {"queued": 1, "processing": 0, "done": 0, "failed": 0}
    manager.claim()
    assert manager.status() == {"queued": 0, "processing": 1, "done": 0, "failed": 0}
    manager.finish(job, ok=True)
    assert manager.status() == {"queued": 0, "processing": 0, "done": 1, "failed": 0}


# -- worker loop -------------------------------------------------------------


def test_run_worker_one_job(
    manager: QueueManager, settings: Settings, job: JobSpec
) -> None:
    """run_worker claims, runs, finishes one job."""
    from storyforge.queue import run_worker

    calls: list[JobSpec] = []

    def run_one(j: JobSpec) -> tuple[bool, str | None]:
        calls.append(j)
        return True, None

    processed = run_worker(settings, run_one, loop=False, max_jobs=1)
    assert processed == 1
    assert len(calls) == 1
    assert calls[0].id == job.id
    # Job moved to done.
    assert (manager.done_dir / f"{job.id}.yaml").exists()


def test_run_worker_heartbeat_runs_during_job(
    manager: QueueManager, settings: Settings, job: JobSpec
) -> None:
    """Heartbeat is called while the job runs (lease is renewed)."""
    settings.queue.heartbeat_interval_seconds = 1
    settings.queue.lease_ttl_seconds = 300
    from storyforge.queue import run_worker

    def run_one(_: JobSpec) -> tuple[bool, str | None]:
        time.sleep(0.5)  # time for heartbeat thread to fire
        return True, None

    processed = run_worker(settings, run_one, loop=False, max_jobs=1)
    assert processed == 1


def test_run_worker_loop_until_empty(
    manager: QueueManager, settings: Settings, job: JobSpec
) -> None:
    """Loop mode processes queued jobs until queue is empty, then continues but
    reclaim_expired returns 0, and max_jobs limits processed count."""
    # Add a second job.
    j2 = new_job("test_proj", universe="test_universe")
    j2.save(settings.queue.queue_dir / f"{j2.id}.yaml")

    from storyforge.queue import run_worker

    processed_ids: list[str] = []

    def run_one(j: JobSpec) -> tuple[bool, str | None]:
        processed_ids.append(j.id)
        return True, None

    # max_jobs in loop mode: returns after processing that many.
    processed = run_worker(settings, run_one, loop=True, interval_seconds=1, max_jobs=2)
    assert processed == 2
    assert len(processed_ids) == 2
    assert job.id in processed_ids
    assert j2.id in processed_ids


# -- JobSpec helpers ---------------------------------------------------------


def test_new_job_has_unique_id() -> None:
    j1 = new_job("p1")
    j2 = new_job("p1")
    assert j1.id != j2.id
    assert j1.project == "p1"
    assert j2.project == "p1"


def test_job_save_roundtrip(tmp_path: Path) -> None:
    j = new_job("p1", universe="u1", urls=["https://example.com"])
    path = tmp_path / "test_job.yaml"
    j.save(path)
    restored = type(j).load(path)
    assert restored.id == j.id
    assert restored.project == j.project
    assert restored.universe == j.universe
    assert restored.urls == j.urls == ["https://example.com"]


def test_job_save_roundtrip_license(tmp_path: Path) -> None:
    """M3-21: license field is persisted and round-tripped."""
    j = new_job("p1", license="cc0", universe="test_u")
    path = tmp_path / "license_job.yaml"
    j.save(path)
    restored = type(j).load(path)
    assert restored.license == "cc0"
    assert j.license == "cc0"


def test_enqueue_creates_yaml(manager: QueueManager, job: JobSpec) -> None:
    path = manager.enqueue(job)
    assert path.exists()
    assert path.suffix == ".yaml"
    assert job.id in str(path)


# -- Lease helpers -----------------------------------------------------------


def test_lease_expired() -> None:
    lease = Lease("w1", 1, 1)
    lease.expires_at = time.time() - 10
    assert lease.expired is True
    lease.expires_at = time.time() + 3600
    assert lease.expired is False


def test_lease_roundtrip() -> None:
    lease = Lease("w1", 42, 300)
    data = lease.to_dict()
    restored = Lease.from_dict(data)
    assert restored.worker_id == "w1"
    assert restored.pid == 42
    assert abs(restored.expires_at - lease.expires_at) < 0.01
