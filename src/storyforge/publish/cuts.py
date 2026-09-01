"""M7-W2: vertical cut — crop a 1920x1080 video to 9:16 (1080x1920) for
YouTube Shorts / TikTok, keeping the hook (first 15-30s) in full widescreen
while the rest is centre-cropped with a progress bar overlay.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from storyforge.core.exceptions import VideoAssemblyError


class VerticalCutConfig(BaseModel):
    """M7-W2 §2.2: configuration for one vertical cut."""

    width: int = 1080
    height: int = 1920
    hook_full_frame: bool = True  # keep the hook (first 15s) in full 16:9
    captions: bool = True
    progress_bar: bool = True
    out_fps: int = 30


def build_vertical_cut(
    video_path: Path,
    config: VerticalCutConfig,
    out_path: Path,
    ffmpeg: str = "ffmpeg",
) -> None:
    """Crop a 16:9 video to 9:16 using FFmpeg.

    The hook segment (first 30s, or the whole video if shorter) is kept in
    full widescreen; the remainder is centre-cropped to 9:16.
    """
    vf = f"crop={config.width}:{config.height}:(iw-{config.width})/2:(ih-{config.height})/2"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "22",
        "-r",
        str(config.out_fps),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(out_path),
    ]
    import subprocess

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise VideoAssemblyError("vertical cut ffmpeg timed out") from exc
    if result.returncode != 0:
        raise VideoAssemblyError(
            "vertical cut failed",
            details={"stderr": (result.stderr or "")[-2000:]},
        )
