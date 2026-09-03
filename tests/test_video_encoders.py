"""Hardware-accelerated encoder tests (M3-V5 §6)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from storyforge.providers.video_encoders import (
    encode_flags,
    probe_encoder,
    reset_encoder_cache,
    resolve_encoder,
)


@pytest.fixture(autouse=True)
def _isolate_probe_memo() -> None:
    """Each test probes a fresh process cache."""
    reset_encoder_cache()
    yield


class _FakeCompletedProcess:
    """Stub for subprocess.CompletedProcess used in probe tests."""

    def __init__(self, stdout: str, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


def _mock_run(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    def fake_run(
        cmd: list[str], **kwargs: object
    ) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_probe_encoder_nvenc_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, "ffmpeg encoders:\n ... h264_nvenc ...")
    assert probe_encoder("ffmpeg") == "h264_nvenc"


def test_probe_encoder_qsv_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, "ffmpeg encoders:\n ... h264_qsv ...")
    assert probe_encoder("ffmpeg") == "h264_qsv"


def test_probe_encoder_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, "no hw encoders here")
    assert probe_encoder("ffmpeg") == "libx264"


def test_probe_encoder_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise OSError("ffmpeg not found")

    monkeypatch.setattr(subprocess, "run", fail)
    assert probe_encoder("missing") == "libx264"


def test_encode_flags_libx264() -> None:
    assert encode_flags("libx264", 20, "medium") == [
        "-c:v", "libx264", "-crf", "20", "-preset", "medium",
    ]


def test_encode_flags_nvenc() -> None:
    flags = encode_flags("h264_nvenc", 20, "medium")
    assert "-c:v" in flags
    assert "h264_nvenc" in flags
    assert "-cq" in flags
    assert "20" in flags


def test_encode_flags_qsv() -> None:
    flags = encode_flags("h264_qsv", 20, "medium")
    assert "-c:v" in flags
    assert "h264_qsv" in flags
    assert "-global_quality" in flags


def test_resolve_encoder_explicit() -> None:
    enc = resolve_encoder("libx264", "ffmpeg", Path("/tmp"))
    assert enc == "libx264"


def test_resolve_encoder_auto_from_cache(tmp_path: Path) -> None:
    cache = tmp_path / ".video_encoder_cache.json"
    cache.write_text(json.dumps({"encoder": "h264_nvenc"}), encoding="utf-8")
    enc = resolve_encoder("auto", "ffmpeg", tmp_path)
    assert enc == "h264_nvenc"


def test_resolve_encoder_auto_probes_when_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, "h264_qsv")
    enc = resolve_encoder("auto", "ffmpeg", tmp_path)
    assert enc == "h264_qsv"
    # Cache file should have been written.
    cache = tmp_path / ".video_encoder_cache.json"
    assert cache.exists()
    assert json.loads(cache.read_text(encoding="utf-8"))["encoder"] == "h264_qsv"
