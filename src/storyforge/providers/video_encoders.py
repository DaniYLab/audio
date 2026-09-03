"""Hardware-accelerated H.264 encoding (M3-V5 §6).

``VideoSettings.encoder`` selects between libx264 (CPU), h264_nvenc (NVIDIA)
and h264_qsv (Intel). In ``auto`` mode ffmpeg is probed once — result cached
in ``.video_encoder_cache.json`` at the workspace root — and the preference
order is nvenc > qsv > libx264 (§6.1). Quality knobs are mapped per encoder
in ``encode_flags`` (data table, not scattered conditionals in the stages).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

Encoder = Literal["libx264", "h264_nvenc", "h264_qsv"]

_PREFERENCE: tuple[Encoder, ...] = ("h264_nvenc", "h264_qsv", "libx264")
_CACHE_NAME = ".video_encoder_cache.json"

# Per-process probe memo (ffmpeg_bin -> encoder): one VideoStage run calls
# this per scene, but the binary set does not change mid-process.
_PROCESS_CACHE: dict[str, Encoder] = {}


def reset_encoder_cache() -> None:
    """Drop the in-process probe memo (test isolation, config reloads)."""
    _PROCESS_CACHE.clear()


def probe_encoder(ffmpeg_bin: str) -> Encoder:
    """Probe ffmpeg's compiled encoders; libx264 is the guaranteed fallback."""
    cached = _PROCESS_CACHE.get(ffmpeg_bin)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        output = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        output = ""
    for enc in _PREFERENCE:
        if enc in output:
            _PROCESS_CACHE[ffmpeg_bin] = enc
            return enc
    _PROCESS_CACHE[ffmpeg_bin] = "libx264"
    return "libx264"


def encode_flags(encoder: Encoder, crf: int, preset: str) -> list[str]:
    """Map (crf, preset) onto the encoder's CLI (M3 §6.1 conversion table).

    libx264 uses ``-crf``/``-preset``; nvenc ``-cq``/``-preset p5``; qsv
    ``-global_quality``. The preset is dropped where the encoder fixes it.
    """
    if encoder == "libx264":
        return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-cq", str(crf), "-preset", "p5"]
    return ["-c:v", "h264_qsv", "-global_quality", str(crf)]


def resolve_encoder(
    encoder_setting: Literal["auto", "libx264", "h264_nvenc", "h264_qsv"],
    ffmpeg_bin: str,
    workspace_root: Path,
) -> Encoder:
    """Resolve the encoder to use: explicit config wins, ``auto`` probes once.

    The probe result is cached to ``<workspace_root>/.video_encoder_cache.json``
    so subsequent runs skip the subprocess; a missing/corrupt cache re-probes.
    """
    if encoder_setting != "auto":
        return encoder_setting

    cache_path = workspace_root / _CACHE_NAME
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            stored = data.get("encoder")
            if isinstance(stored, str) and stored in _PREFERENCE:
                return stored
        except (OSError, ValueError, json.JSONDecodeError):
            pass  # corrupt cache -> re-probe

    encoder = probe_encoder(ffmpeg_bin)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"encoder": encoder}), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        pass  # cache write is best-effort
    return encoder


__all__ = [
    "Encoder",
    "encode_flags",
    "probe_encoder",
    "reset_encoder_cache",
    "resolve_encoder",
]
