"""Disk lifecycle tests (M3-V4 §5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.cleanup import (
    CleanError,
    apply_report,
    clean_project,
    clean_tts_cache,
    parse_age,
)
from storyforge.core.artifacts import ArtifactStore
from storyforge.core.types import SourceRef, Transcript, TranscriptSegment


def _make_project(tmp_path: Path) -> ArtifactStore:
    store = ArtifactStore(tmp_path / "workspace", "proj")
    # Render intermediates that clean should reclaim.
    (store.dir("logs") / "seg_beat_00.mp4").write_bytes(b"seg")
    (store.dir("logs") / "concat.txt").write_text("file 'x'\n")
    (store.dir("logs") / "ffmpeg.log").write_text("log")
    (store.dir("logs") / "subtitles.srt").write_text("1\n00:00 --> 00:01\nhi\n")
    (store.dir("05_tts") / "beat_00.mp3").write_bytes(b"audio")
    (store.dir("06_images") / "beat_00.png").write_bytes(b"png")
    (store.dir("06_images") / "thumbnail.png").write_bytes(b"thumb")
    (store.dir("01_download") / "abc123.m4a").write_bytes(b"remote")
    (store.dir("01_download") / "local.m4a").write_bytes(b"local")
    return store


def _write_transcript(store: ArtifactStore, source: SourceRef) -> None:
    transcript = Transcript(
        source=source,
        language="vi",
        audio_path=source.local_path or Path("x"),
        segments=[TranscriptSegment(start=0, end=1, text="x")],
    )
    store.write_model(store.transcript_path(source.id), transcript)


def test_parse_age() -> None:
    assert parse_age("7d") == 7 * 86400
    assert parse_age("48h") == 48 * 3600
    assert parse_age("30m") == 30 * 60
    with pytest.raises(CleanError):
        parse_age("nope")
    with pytest.raises(CleanError):
        parse_age("2w")


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    store = _make_project(tmp_path)
    report = clean_project(store, dry_run=True)
    assert report.file_count > 0
    assert (store.dir("logs") / "seg_beat_00.mp4").exists()  # untouched
    assert report.dry_run


def test_default_cleans_logs_keeps_intermediates(tmp_path: Path) -> None:
    store = _make_project(tmp_path)
    apply_report(clean_project(store))
    assert not (store.dir("logs") / "seg_beat_00.mp4").exists()
    assert not (store.dir("logs") / "concat.txt").exists()
    assert (store.dir("logs") / "subtitles.srt").exists()  # SRT always kept
    # 05_tts / 06_images survive without --keep-final.
    assert (store.dir("05_tts") / "beat_00.mp3").exists()
    assert (store.dir("06_images") / "beat_00.png").exists()


def test_keep_final_cleans_intermediates_keeps_thumbnail(tmp_path: Path) -> None:
    store = _make_project(tmp_path)
    (store.dir("07_video") / "final.mp4").write_bytes(b"final")
    apply_report(clean_project(store, keep_final=True))
    assert not (store.dir("05_tts") / "beat_00.mp3").exists()
    assert not (store.dir("06_images") / "beat_00.png").exists()
    # thumbnail survives for publish reuse.
    assert (store.dir("06_images") / "thumbnail.png").exists()


def test_keep_final_requires_final_mp4(tmp_path: Path) -> None:
    store = _make_project(tmp_path)  # no final.mp4
    report = clean_project(store, keep_final=True)
    assert (store.dir("05_tts") / "beat_00.mp3").exists()
    assert any("05_tts/" in s for s in report.skipped_files)


def test_remote_audio_deletable_local_kept(tmp_path: Path) -> None:
    store = _make_project(tmp_path)
    _write_transcript(store, SourceRef(id="abc123", url="https://youtu.be/x"))
    _write_transcript(store, SourceRef(id="local", local_path=Path("local.m4a")))
    apply_report(clean_project(store))
    assert not (store.dir("01_download") / "abc123.m4a").exists()
    assert (store.dir("01_download") / "local.m4a").exists()


def test_older_than_skips_fresh_files(tmp_path: Path) -> None:
    store = _make_project(tmp_path)
    report = clean_project(store, older_than=86400.0)  # 1 day — files are new
    assert report.file_count == 0
    assert any("younger" in s for s in report.skipped_files)


def test_clean_tts_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "tts"
    cache.mkdir(parents=True)
    (cache / "a.mp3").write_bytes(b"x")
    (cache / "b.mp3").write_bytes(b"yy")
    report = clean_tts_cache(cache)
    assert report.file_count == 2
    apply_report(report)
    assert not cache.exists() or not any(cache.iterdir())
