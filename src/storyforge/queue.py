"""Shared job queue for multi-worker processing (M4-A6).

Layout (M4 design §3.2):

    data/queue/
    ├── <job>.yaml            # job spec (+ owner, scheduled_at)
    ├── .leases/<job>.lease   # {"worker_id","pid","expires_at_epoch"}
    ├── .processing/<job>.yaml
    ├── .failed/<job>.yaml  + .error.txt
    └── .done/<job>.yaml

Lease protocol (AC1):
- Claim: rename job → .processing/ (atomic lock) then write a lease with
  ``expires_at = now + ttl``.
- Heartbeat: every interval the running worker extends the lease expiry.
- Recovery: a worker (or its own restart) that finds a .processing/ job whose
  lease has expired reclaims it (stale lock from a crashed worker). PID liveness
  is used as an extra signal where available.

The job body itself is idempotent at stage level, so reclaiming a job that may
have partially run is safe (pipeline skips done stages).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from storyforge.core.config import Settings
from storyforge.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class JobSpec:
    """One queue job — the parameters for a pipeline run (AC3)."""

    id: str
    project: str
    source_config: str = "config/story_config.example.yaml"
    universe: str = ""
    urls: list[str] = field(default_factory=list)
    local_files: list[str] = field(default_factory=list)
    owner: str = "cli"  # AC3
    scheduled_at: float | None = None  # epoch; None = run now
    tier: str = "standard"
    license: str = "unknown"  # M3 §8.3: source content license for the run

    @classmethod
    def load(cls, path: Path) -> JobSpec:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            id=str(data.get("id", path.stem)),
            project=str(data.get("project", "")),
            source_config=str(data.get("source_config", "config/story_config.example.yaml")),
            universe=str(data.get("universe", "")),
            urls=[str(u) for u in data.get("urls", [])],
            local_files=[str(f) for f in data.get("local_files", [])],
            owner=str(data.get("owner", "cli")),
            scheduled_at=data.get("scheduled_at"),
            tier=str(data.get("tier", "standard")),
            license=str(data.get("license", "unknown")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.model_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def model_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "project": self.project,
            "source_config": self.source_config,
            "universe": self.universe,
            "urls": self.urls,
            "local_files": self.local_files,
            "owner": self.owner,
            "scheduled_at": self.scheduled_at,
            "tier": self.tier,
            "license": self.license,
        }


class Lease:
    """JSON lease record for one processing job."""

    def __init__(self, worker_id: str, pid: int, ttl_seconds: int) -> None:
        self.worker_id = worker_id
        self.pid = pid
        self.expires_at = time.time() + ttl_seconds

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "pid": self.pid,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Lease:
        lease = cls(str(data.get("worker_id", "")), int(str(data.get("pid", 0))), 0)
        lease.expires_at = float(str(data.get("expires_at", 0)))
        return lease


try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    psutil = None


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check.

    NOTE: os.kill(pid, 0) is NOT safe on Windows (it calls TerminateProcess).
    Stick to psutil globally; fall back to "assume alive" when unavailable.
    """
    if psutil is not None:
        return bool(psutil.pid_exists(pid))
    return True  # cannot verify — assume alive (lease TTL covers it)


class QueueManager:
    """Filesystem queue with lease-based claim/reclaim (AC1)."""

    def __init__(self, settings: Settings) -> None:
        q = settings.queue
        self.queue_dir = q.queue_dir
        self.leases_dir = self.queue_dir / ".leases"
        self.processing_dir = self.queue_dir / ".processing"
        self.failed_dir = self.queue_dir / ".failed"
        self.done_dir = self.queue_dir / ".done"
        self.ttl = q.lease_ttl_seconds
        self.heartbeat = q.heartbeat_interval_seconds
        self.scheduled_grace = q.scheduled_grace_seconds
        self.worker_id = q.worker_id or os.environ.get(
            "SF__QUEUE__WORKER_ID", os.uname().nodename if hasattr(os, "uname") else "worker"
        )
        for d in (self.queue_dir, self.leases_dir, self.processing_dir, self.failed_dir, self.done_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- queue listing -------------------------------------------------------

    def enqueue(self, job: JobSpec) -> Path:
        """Add a job to the queued bucket (write the spec file)."""
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        path = self.queue_dir / f"{job.id}.yaml"
        job.save(path)
        logger.info("job enqueued", job=job.id, project=job.project)
        return path

    def queued(self) -> list[Path]:
        return sorted(self.queue_dir.glob("*.yaml"))

    def processing(self) -> list[Path]:
        return sorted(self.processing_dir.glob("*.yaml"))

    def done(self) -> list[Path]:
        return sorted(self.done_dir.glob("*.yaml"))

    def failed(self) -> list[Path]:
        return sorted(self.failed_dir.glob("*.yaml"))

    # -- claim / heartbeat / reclaim (AC1) ------------------------------------

    def claim(self) -> JobSpec | None:
        """Claim the next due job; None when queue is empty or all claimed."""
        for job_path in self.queued():
            job = JobSpec.load(job_path)
            if job.scheduled_at and job.scheduled_at > time.time() + self.scheduled_grace:
                continue  # not due yet
            target = self.processing_dir / job_path.name
            try:
                job_path.replace(target)  # atomic rename = the lock
            except FileNotFoundError:
                continue  # someone else claimed it
            lease = Lease(self.worker_id, os.getpid(), self.ttl)
            self._write_lease(job.id, lease)
            logger.info("job claimed", job=job.id, worker=self.worker_id)
            return job
        return None

    def heartbeat_for(self, job_id: str) -> None:
        """Extend the lease of a job this worker is running."""
        lease_path = self.leases_dir / f"{job_id}.lease"
        if not lease_path.exists():
            return
        lease = Lease(self.worker_id, os.getpid(), self.ttl)
        self._write_lease(job_id, lease)

    def _write_lease(self, job_id: str, lease: Lease) -> None:
        self.leases_dir.mkdir(parents=True, exist_ok=True)
        (self.leases_dir / f"{job_id}.lease").write_text(
            json.dumps(lease.to_dict()), encoding="utf-8"
        )

    def reclaim_expired(self) -> int:
        """Reclaim processing jobs whose lease expired (stale lock recovery)."""
        reclaimed = 0
        for job_path in self.processing():
            lease_path = self.leases_dir / f"{job_path.stem}.lease"
            if not lease_path.exists():
                continue
            try:
                lease = Lease.from_dict(json.loads(lease_path.read_text(encoding="utf-8")))
            except (ValueError, json.JSONDecodeError):
                continue
            # PID dead OR lease expired -> reclaimable.
            if lease.pid != os.getpid() and (not _pid_alive(lease.pid) or lease.expired):
                # Move back to queue for another worker to claim (it is
                # idempotent at stage level).
                job_path.replace(self.queue_dir / job_path.name)
                with suppress(OSError):
                    (self.leases_dir / f"{job_path.stem}.lease").unlink()
                reclaimed += 1
                logger.warning("job reclaimed (stale lease)", job=job_path.stem)
        return reclaimed

    # -- finish ---------------------------------------------------------------

    def finish(self, job: JobSpec, *, ok: bool, error: str | None = None) -> None:
        src = self.processing_dir / f"{job.id}.yaml"
        if ok:
            src.replace(self.done_dir / f"{job.id}.yaml")
        else:
            src.replace(self.failed_dir / f"{job.id}.yaml")
            if error:
                (self.failed_dir / f"{job.id}.error.txt").write_text(error, encoding="utf-8")
        with suppress(OSError):
            (self.leases_dir / f"{job.id}.lease").unlink()

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued (not processing) job (AC3)."""
        path = self.queue_dir / f"{job_id}.yaml"
        if not path.exists():
            return False
        path.unlink()
        return True

    def status(self) -> dict[str, int]:
        """Counts per bucket + lease-expired note for the CLI table."""
        return {
            "queued": len(self.queued()),
            "processing": len(self.processing()),
            "done": len(self.done()),
            "failed": len(self.failed()),
        }


def run_worker(
    settings: Settings,
    run_one: Callable[[JobSpec], tuple[bool, str | None]],
    *,
    loop: bool = False,
    interval_seconds: int = 30,
    max_jobs: int | None = None,
) -> int:
    """Worker loop (AC2). ``run_one(job)`` returns (ok, error).

    Heartbeats the lease while the job runs; reclaims stale leases each tick.
    """
    manager = QueueManager(settings)
    manager.reclaim_expired()
    processed = 0
    while True:
        if max_jobs is not None and processed >= max_jobs:
            return processed
        job = manager.claim()
        if job is None:
            if not loop:
                return processed
            time.sleep(interval_seconds)
            manager.reclaim_expired()
            continue
        # Heartbeat loop in a thread-lite manner: run the job, extend lease.
        ok, error = _run_with_heartbeat(manager, job, run_one)
        manager.finish(job, ok=ok, error=error)
        processed += 1
        if not loop:
            return processed


def _run_with_heartbeat(
    manager: QueueManager, job: JobSpec, run_one: Callable[[JobSpec], tuple[bool, str | None]]
) -> tuple[bool, str | None]:
    """Run one job while refreshing the lease every heartbeat interval."""
    import threading

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(manager.heartbeat):
            manager.heartbeat_for(job.id)

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        return run_one(job)
    finally:
        stop.set()
        t.join(timeout=1)


def new_job(project: str, **kwargs: object) -> JobSpec:
    """Create a JobSpec with a unique id (helper for tests + CLI)."""
    return JobSpec(id=f"{project}_{uuid.uuid4().hex[:8]}", project=project, **kwargs)  # type: ignore[arg-type]
