"""M4-A5 / T3-DEV1: music library tests — moods.yaml resolver + add + CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.music import (
    MusicLibraryError,
    add_mood,
    load_moods,
    resolve_mood_file,
)
from storyforge.stages.video import VideoStage


# -- resolve via moods.yaml (T3-DEV1 AC2/AC3) ---------------------------------


def _write_moods(path: Path, moods: dict[str, dict[str, object]]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"moods": moods}), encoding="utf-8")


def _music_file(tmp_path: Path, name: str = "calm.mp3") -> Path:
    music = tmp_path / "assets" / "music_cc0"
    music.mkdir(parents=True)
    f = music / name
    f.write_bytes(b"fake-audio")
    return f


def test_resolve_missing_mood_returns_none() -> None:
    assert resolve_mood_file("nonexistent-mood") is None


def test_resolve_existing_file(tmp_path: Path) -> None:
    file = _music_file(tmp_path)
    moods = tmp_path / "config" / "music_moods.yaml"
    _write_moods(moods, {"calm": {"file": str(file), "volume": 0.15, "license": "cc0"}})
    resolved = resolve_mood_file("calm", moods_path=moods)
    assert resolved is not None
    assert resolved.name == "calm.mp3"


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    moods = tmp_path / "config" / "music_moods.yaml"
    _write_moods(
        moods,
        {"calm": {"file": str(tmp_path / "no_such.mp3"), "volume": 0.15, "license": ""}},
    )
    assert resolve_mood_file("calm", moods_path=moods) is None


def test_resolve_none_mood_returns_none() -> None:
    stage = VideoStage(clips=[], illustrations=[], music_mood=None)
    assert stage._resolve_music(type("Ctx", (), {})()) is None  # type: ignore[arg-type]


# -- add_mood (T3-DEV1 AC1) ---------------------------------------------------


def test_add_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MusicLibraryError, match="not found"):
        add_mood(tmp_path / "missing.mp3", "calm", "https://example.com/lic")


def test_add_rejects_missing_license(tmp_path: Path) -> None:
    src = tmp_path / "calm.mp3"
    src.write_bytes(b"fake")
    with pytest.raises(MusicLibraryError, match="license"):
        add_mood(src, "calm", "")


def test_add_copies_and_records(tmp_path: Path) -> None:
    src = tmp_path / "src_calm.mp3"
    src.write_bytes(b"audio-bytes")
    moods_path = tmp_path / "config" / "music_moods.yaml"
    music_dir = tmp_path / "assets" / "music_cc0"

    entry = add_mood(src, "calm", "https://cc0.example/x", moods_path=moods_path, music_dir=music_dir)
    assert entry.mood == "calm"
    assert (music_dir / "calm.mp3").exists()
    assert (music_dir / "calm.mp3").read_bytes() == b"audio-bytes"
    # LICENSES.md created with the license line.
    lic = (music_dir / "LICENSES.md").read_text(encoding="utf-8")
    assert "https://cc0.example/x" in lic
    # moods.yaml updated.
    moods = load_moods(moods_path)
    assert any(m.mood == "calm" and m.license == "https://cc0.example/x" for m in moods)


def test_add_overwrites_existing_mood(tmp_path: Path) -> None:
    src = tmp_path / "a.mp3"
    src.write_bytes(b"new")
    moods_path = tmp_path / "config" / "music_moods.yaml"
    music_dir = tmp_path / "assets" / "music_cc0"
    add_mood(src, "calm", "https://a", moods_path=moods_path, music_dir=music_dir)
    src.write_bytes(b"newer")
    add_mood(src, "calm", "https://b", moods_path=moods_path, music_dir=music_dir)
    moods = load_moods(moods_path)
    assert sum(1 for m in moods if m.mood == "calm") == 1
    assert moods[0].license == "https://b"


def test_load_moods_absent_returns_empty(tmp_path: Path) -> None:
    assert load_moods(tmp_path / "missing.yaml") == []


# -- VideoStage._audio_filter unchanged ---------------------------------------


def test_audio_filter_builds_aloop_amix() -> None:
    stage = VideoStage(clips=[], illustrations=[], music_mood="calm")
    filtergraph = stage._audio_filter(None, Path("assets/music_cc0/calm.mp3"))
    assert filtergraph is not None
    assert "aloop" in filtergraph
    assert "amix=inputs=2:duration=first" in filtergraph
    assert "volume=0.15" in filtergraph