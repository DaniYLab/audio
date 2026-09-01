"""M4-A4: Publish receipt — tracks what was uploaded and when.

The receipt lives in ``07_video/publish.json`` alongside ``final.mp4``.
Idempotency check (AC3): if the same file hash and privacy level already
exist, the publish step is a no-op.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from storyforge.core.types import utc_now


class PublishReceipt(BaseModel):
    """Record of one YouTube upload (A4 §1.2)."""

    project: str
    video_id: str  # YouTube video id
    privacy: str  # "private", "unlisted", "public"
    uploaded_at: datetime = Field(default_factory=utc_now)
    file_hash: str  # sha256 of final.mp4 — prevents duplicate uploads
    url: str  # https://youtu.be/<video_id>

    @staticmethod
    def hash_file(path: Path) -> str:
        """Return the hex sha256 of a file."""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                block = fh.read(65536)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()


def load_receipt(path: Path) -> PublishReceipt | None:
    """Load publish receipt from ``07_video/publish.json``.

    Returns None when the file does not exist or is malformed.
    """
    if not path.exists():
        return None
    try:
        return PublishReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def save_receipt(path: Path, receipt: PublishReceipt) -> None:
    """Atomically write a publish receipt."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def should_upload(
    receipt_path: Path,
    video_path: Path,
    privacy: str,
) -> bool:
    """Idempotency check: skip upload when receipt matches.

    Returns True when the video should be uploaded (no matching receipt).
    Returns False when a receipt with the same file hash and the same or
    stricter privacy exists.
    """
    receipt = load_receipt(receipt_path)
    if receipt is None:
        return True
    if not video_path.exists():
        return True
    current_hash = PublishReceipt.hash_file(video_path)
    if receipt.file_hash != current_hash:
        return True  # file changed — re-upload
    # Privacy is considered "already satisfied" when the receipt's privacy
    # is at least as permissive as the requested one (e.g. public already
    # satisfies a request for private; private does NOT satisfy a request
    # for public — upgrade needed).
    privacy_rank = {"private": 0, "unlisted": 1, "public": 2}
    return privacy_rank.get(receipt.privacy, 0) < privacy_rank.get(privacy, 0)
