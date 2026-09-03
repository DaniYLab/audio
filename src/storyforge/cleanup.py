"""Disk lifecycle management (M3-V4 §5, M4 §5.1).

``storyforge clean`` reclaims intermediate artifacts after the final render.

Policy (per design table):

| Component | Action |
|---|---|
| ``01_download/`` | keep when the source is a local file (not recoverable); deletable when the transcript carries a URL (re-downloadable) |
| ``02_transcripts/``, ``04_story/``, ``07_video/final.mp4``, ``manifest.json`` | always kept |
| ``05_tts/``, ``06_images/`` | deleted only with ``keep_final=True`` AND the final video exists — the final render is the receipt that the intermediates are reclaimable. ``thumbnail.png`` survives (publish reuses it). |
| ``logs/seg_*.mp4``, ``logs/concat.txt``, ffmpeg logs | deleted (``subtitles.srt`` kept) |

``--dry-run`` reports what would be deleted without touching anything;
``--older-than`` limits deletion to files older than a duration (``7d``/``48h``/
``30m``). The batch runner calls ``clean_project(..., keep_final=True)`` after
every successful job (M3 §5.2).
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.exceptions import StoryForgeError
from storyforge.core.logging import get_logger

logger = get_logger(__name__)

# Log files that are safe to delete once a render finished.
_LOG_GLOBS = ("seg_*.mp4", "concat.txt", "ffmpeg*.log")
# Files inside logs/ that are never deleted.
_KEEP_LOG_FILES = {"subtitles.srt"}

_AGE_RE = re.compile(r"^(\d+)([dhm])$")


class CleanError(StoryForgeError):
    """Invalid clean request (bad age string, missing project)."""


class CleanReport(BaseModel):
    """Result of one clean invocation (also the dry-run preview)."""

    project: str
    dry_run: bool = False
    deleted_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    freed_bytes: int = 0

    @property
    def file_count(self) -> int:
        return len(self.deleted_files)


def parse_age(age: str) -> float:
    """Parse ``7d`` / ``48h`` / ``30m`` into seconds (M3 §5.2)."""
    match = _AGE_RE.fullmatch(age.strip())
    if match is None:
        raise CleanError(f"invalid --older-than value: {age!r} (use e.g. 7d, 48h, 30m)")
    value, unit = int(match.group(1)), match.group(2)
    return value * {"d": 86400.0, "h": 3600.0, "m": 60.0}[unit]


def clean_project(
    store: ArtifactStore,
    *,
    keep_final: bool = False,
    dry_run: bool = False,
    older_than: float | None = None,
) -> CleanReport:
    """Delete reclaimable intermediates of one project.

    ``keep_final=True`` authorizes deleting ``05_tts/`` + ``06_images/`` once
    ``07_video/final.mp4`` exists (the batch-runner mode). Never raises for
    missing paths — missing artifacts are simply not deleted.
    """
    report = CleanReport(project=store.root.name, dry_run=dry_run)
    root = store.root

    # logs/ — segments, concat list and ffmpeg logs always go; SRT stays.
    log_dir = root / "logs"
    if log_dir.exists():
        for pattern in _LOG_GLOBS:
            for path in sorted(log_dir.glob(pattern)):
                _collect_delete(path, report, older_than)

    # 01_download/ — a transcript with a source URL is re-downloadable.
    deletable_sources = _redownloadable_source_ids(store)
    audio_dir = root / "01_download"
    if audio_dir.exists():
        for path in sorted(p for p in audio_dir.iterdir() if p.is_file()):
            if path.stem in deletable_sources:
                _collect_delete(path, report, older_than)
            else:
                report.skipped_files.append(str(path))

    # 05_tts/ + 06_images/ — only when the final render is kept (keep_final)
    # AND actually exists; otherwise a resume would have to regenerate them.
    final_video = store.video_path()
    if keep_final and final_video.exists():
        for stage_dir in ("05_tts", "06_images"):
            directory = root / stage_dir
            if not directory.exists():
                continue
            for path in sorted(p for p in directory.iterdir() if p.is_file()):
                # thumbnail.png rides along — publish reuses it after cleanup.
                if stage_dir == "06_images" and path.name == "thumbnail.png":
                    report.skipped_files.append(str(path))
                    continue
                _collect_delete(path, report, older_than)
    else:
        for stage_dir in ("05_tts", "06_images"):
            directory = root / stage_dir
            if directory.exists() and any(directory.iterdir()):
                report.skipped_files.append(f"{stage_dir}/ (needs --keep-final + final.mp4)")

    return report


def clean_tts_cache(cache_dir: Path, *, dry_run: bool = False) -> CleanReport:
    """Delete the TTS audio cache (M3-W3 §8.2 ``clean --tts-cache``)."""
    report = CleanReport(project=str(cache_dir), dry_run=dry_run)
    if not cache_dir.exists():
        return report
    for path in sorted(p for p in cache_dir.iterdir() if p.is_file()):
        _collect_delete(path, report, None)
    return report


def apply_report(report: CleanReport) -> CleanReport:
    """Delete the files listed in a dry-run report (no-op on dry_run)."""
    if report.dry_run:
        return report
    for raw in report.deleted_files:
        path = Path(raw)
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("clean failed to delete", path=str(path), error=str(exc))
    return report


def _redownloadable_source_ids(store: ArtifactStore) -> set[str]:
    """Source ids whose transcript carries a remote URL (safe to re-fetch)."""
    transcript_dir = store.root / "02_transcripts"
    if not transcript_dir.exists():
        return set()
    out: set[str] = set()
    for path in sorted(transcript_dir.glob("*.json")):
        try:
            from storyforge.core.types import Transcript

            transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if transcript.source.url:
            out.add(transcript.source.id)
    return out


def _collect_delete(path: Path, report: CleanReport, older_than: float | None) -> None:
    """Add ``path`` to the report (age-filtered); frees nothing by itself."""
    if older_than is not None:
        try:
            age_seconds = __import__("time").time() - path.stat().st_mtime
        except OSError:
            report.skipped_files.append(str(path))
            return
        if age_seconds < older_than:
            report.skipped_files.append(f"{path} (younger than --older-than)")
            return
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    report.deleted_files.append(str(path))
    report.freed_bytes += size
