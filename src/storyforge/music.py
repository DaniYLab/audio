"""M4-A5 / T3-DEV1: music library manager.

Moods live in ``config/music_moods.yaml`` (mood → file/volume/license); the
audio files live in ``assets/music_cc0/``. ``--add`` copies the file into the
library, probes its duration with ffprobe, and requires a license URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from storyforge.core.exceptions import StoryForgeError

MOODS_PATH = Path("config/music_moods.yaml")
MUSIC_DIR = Path("assets/music_cc0")
LICENSES_PATH = MUSIC_DIR / "LICENSES.md"


@dataclass
class MusicMood:
    """One mood entry from music_moods.yaml."""

    mood: str
    file: str
    volume: float = 0.15
    license: str = ""
    duration_seconds: float | None = None


class MusicLibraryError(StoryForgeError):
    """Invalid --add request (missing file, missing license, …)."""


def load_moods(path: Path | None = None) -> list[MusicMood]:
    """Read music_moods.yaml into a list of MusicMood (empty when absent)."""
    path = path or MOODS_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    moods: list[MusicMood] = []
    for mood, cfg in (data.get("moods") or {}).items():
        if not isinstance(cfg, dict):
            continue
        moods.append(
            MusicMood(
                mood=str(mood),
                file=str(cfg.get("file", "")),
                volume=float(cfg.get("volume", 0.15) or 0.15),
                license=str(cfg.get("license", "") or ""),
            )
        )
    return moods


def save_moods(moods: list[MusicMood], path: Path | None = None) -> None:
    """Write the mood list back to music_moods.yaml."""
    path = path or MOODS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "moods": {
            m.mood: {
                "file": m.file,
                "volume": m.volume,
                "license": m.license,
            }
            for m in moods
        }
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def probe_duration(path: Path, ffprobe: str = "ffprobe") -> float | None:
    """Best-effort audio duration via ffprobe (None when ffprobe missing)."""
    import json
    import subprocess

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def add_mood(
    source: Path,
    mood: str,
    license_url: str,
    *,
    moods_path: Path | None = None,
    music_dir: Path | None = None,
    ffprobe: str = "ffprobe",
) -> MusicMood:
    """Add a CC0 file to the library (T3-DEV1 AC1).

    Rejects when the file does not exist or no license URL is provided.
    Copies the file into ``music_cc0/<mood>.mp3`` and records the mood.
    """
    if not source.exists():
        raise MusicLibraryError(f"music file not found: {source}")
    if not license_url.strip():
        raise MusicLibraryError("a license URL is required (--license) — no file without license")

    music_dir = music_dir or MUSIC_DIR
    dest = music_dir / f"{mood}.mp3"
    music_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(source, dest)

    duration = probe_duration(dest, ffprobe)

    moods = load_moods(moods_path)
    entry = MusicMood(mood=mood, file=str(dest), volume=0.15, license=license_url)
    if duration is not None:
        entry.duration_seconds = duration
    moods = [m for m in moods if m.mood != mood] + [entry]
    save_moods(moods, moods_path)
    _append_license(music_dir, mood, source.name, license_url)
    return entry


def _append_license(music_dir: Path, mood: str, source_name: str, url: str) -> None:
    """Append a license line to LICENSES.md (create with a header)."""
    path = music_dir / "LICENSES.md"
    if not path.exists():
        path.write_text("# CC0 music licenses\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"- {mood}.mp3 (from {source_name}) — {url}\n")


def resolve_mood_file(mood: str, *, moods_path: Path | None = None) -> Path | None:
    """Resolve a mood to its audio file path (None when mood/file missing)."""
    for entry in load_moods(moods_path):
        if entry.mood == mood:
            path = Path(entry.file)
            return path if path.exists() else None
    return None
