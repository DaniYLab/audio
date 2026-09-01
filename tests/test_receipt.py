"""M4-A4: publish receipt unit tests — idempotency (AC3)."""

from __future__ import annotations

from pathlib import Path

from storyforge.publish.receipt import (
    PublishReceipt,
    load_receipt,
    save_receipt,
    should_upload,
)


def test_hash_file_stable(tmp_path: Path) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"hello world" * 100)
    h1 = PublishReceipt.hash_file(video)
    h2 = PublishReceipt.hash_file(video)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_hash_changes_with_content(tmp_path: Path) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"aaa")
    h1 = PublishReceipt.hash_file(video)
    video.write_bytes(b"bbb")
    h2 = PublishReceipt.hash_file(video)
    assert h1 != h2


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    receipt = PublishReceipt(
        project="proj",
        video_id="vid123",
        privacy="private",
        file_hash="abc",
        url="https://youtu.be/vid123",
    )
    path = tmp_path / "publish.json"
    save_receipt(path, receipt)
    loaded = load_receipt(path)
    assert loaded is not None
    assert loaded.video_id == "vid123"
    assert loaded.privacy == "private"


def test_load_receipt_missing_returns_none(tmp_path: Path) -> None:
    assert load_receipt(tmp_path / "publish.json") is None


def test_load_receipt_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "publish.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_receipt(path) is None


def test_should_upload_no_receipt(tmp_path: Path) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 100)
    assert should_upload(tmp_path / "publish.json", video, "private") is True


def test_should_upload_skip_when_same_hash(tmp_path: Path) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 100)
    receipt_path = tmp_path / "publish.json"
    save_receipt(
        receipt_path,
        PublishReceipt(
            project="proj",
            video_id="vid123",
            privacy="private",
            file_hash=PublishReceipt.hash_file(video),
            url="https://youtu.be/vid123",
        ),
    )
    assert should_upload(receipt_path, video, "private") is False


def test_should_upload_upload_when_hash_differs(tmp_path: Path) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 100)
    receipt_path = tmp_path / "publish.json"
    save_receipt(
        receipt_path,
        PublishReceipt(
            project="proj",
            video_id="vid123",
            privacy="private",
            file_hash="different-hash",
            url="https://youtu.be/vid123",
        ),
    )
    assert should_upload(receipt_path, video, "private") is True


def test_should_upload_upgrade_privacy(tmp_path: Path) -> None:
    """Receipt at private, requesting public → must re-upload."""
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 100)
    receipt_path = tmp_path / "publish.json"
    save_receipt(
        receipt_path,
        PublishReceipt(
            project="proj",
            video_id="vid123",
            privacy="private",
            file_hash=PublishReceipt.hash_file(video),
            url="https://youtu.be/vid123",
        ),
    )
    assert should_upload(receipt_path, video, "public") is True
