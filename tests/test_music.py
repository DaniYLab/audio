"""M4-A5 music manager tests — resolve + list (no audio files needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.stages.video import VideoStage


def test_music_resolve_missing_mood_returns_none():
    assert (
        VideoStage(clips=[], illustrations=[])._resolve_music(
            type("Ctx", (), {})()  # type: ignore[arg-type]
        )
        is None
    )


def test_music_resolve_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    music = tmp_path / "assets" / "music_cc0"
    music.mkdir(parents=True)
    (music / "warm.mp3").write_bytes(b"fake")
    monkeypatch.setattr("storyforge.stages.video.MUSIC_DIR", music)

    stage = VideoStage(clips=[], illustrations=[], music_mood="warm")
    resolved = stage._resolve_music(type("Ctx", (), {})())  # type: ignore[arg-type]
    assert resolved is not None and resolved.name == "warm.mp3"


def test_music_resolve_missing_file_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    music = tmp_path / "assets" / "music_cc0"
    music.mkdir(parents=True)
    monkeypatch.setattr("storyforge.stages.video.MUSIC_DIR", music)

    stage = VideoStage(clips=[], illustrations=[], music_mood="tense")
    assert stage._resolve_music(type("Ctx", (), {})()) is None  # type: ignore[arg-type]


def test_audio_filter_returns_none_without_music():
    stage = VideoStage(clips=[], illustrations=[])
    assert stage._audio_filter(None, None) is None


def test_audio_filter_builds_aloop_amix():
    stage = VideoStage(clips=[], illustrations=[], music_mood="warm")
    filtergraph = stage._audio_filter(None, Path("assets/music_cc0/warm.mp3"))
    assert filtergraph is not None
    assert "aloop" in filtergraph
    assert "amix=inputs=2:duration=first" in filtergraph
    assert "volume=0.15" in filtergraph
