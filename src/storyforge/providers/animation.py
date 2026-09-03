"""Animation provider — image-to-video (M6-W1).

``AnimationProvider`` protocol turns a still image into a short motion clip
that the video stage assembles. The built-in ``KenBurnsFallback`` uses the
existing FFmpeg zoompan filter (cost 0, always available); API providers
(FalKling, Veo) plug in behind the same interface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from storyforge.core.config import Settings
from storyforge.core.exceptions import VideoAssemblyError
from storyforge.providers.video_encoders import encode_flags, resolve_encoder


class MotionSpec(BaseModel):
    """Motion description for one animated scene (M6-W1 §2.1)."""

    kind: Literal["kenburns", "slow_push", "pan", "subtle_zoom", "particles"] = "kenburns"
    intensity: float = 0.3  # 0.0 = static, 1.0 = full


class AnimatedClip(BaseModel):
    """Output of one animation call."""

    clip_path: Path
    duration_seconds: float
    provider: str  # "fal_kling" | "veo" | "kenburns_fallback"
    cost_usd: float = 0.0


@runtime_checkable
class AnimationProvider(Protocol):
    """Turn a still image into a motion clip."""

    def animate(
        self,
        image_path: str,
        duration_seconds: float,
        motion: MotionSpec,
        out_path: str,
    ) -> AnimatedClip: ...


class KenBurnsFallback:
    """FFmpeg zoompan — zero cost, always available (M6-W1 fallback)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def animate(
        self,
        image_path: str,
        duration_seconds: float,
        motion: MotionSpec,
        out_path: str,
    ) -> AnimatedClip:
        frames = int(duration_seconds * 30) + 1
        vf = (
            f"zoompan=z='1+{motion.intensity:.1f}*on/{frames}':"
            f"d={frames}:s={self._settings.imaging.width}x{self._settings.imaging.height}:fps=30"
        )
        cmd = [
            self._settings.video.ffmpeg_bin,
            "-y",
            "-loop",
            "1",
            "-i",
            image_path,
            "-vf",
            vf,
            "-t",
            str(duration_seconds),
            *encode_flags(
                resolve_encoder(
                    self._settings.video.encoder,
                    self._settings.video.ffmpeg_bin,
                    self._settings.workspace_dir,
                ),
                self._settings.video.crf,
                self._settings.video.preset,
            ),
            "-pix_fmt",
            "yuv420p",
            str(out_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
        except subprocess.TimeoutExpired as exc:
            raise VideoAssemblyError("animation ffmpeg timed out") from exc
        if result.returncode != 0:
            raise VideoAssemblyError(
                "animation ffmpeg failed",
                details={"stderr": (result.stderr or "")[-2000:]},
            )
        return AnimatedClip(
            clip_path=Path(out_path),
            duration_seconds=duration_seconds,
            provider="kenburns_fallback",
            cost_usd=0.0,
        )


def build_animation_provider(settings: Settings) -> AnimationProvider:
    """Factory: returns the configured provider, falling back to KenBurns."""
    engine = settings.animation.provider if hasattr(settings, "animation") else "kenburns"
    if engine == "kenburns":
        return KenBurnsFallback(settings)
    # FalKling / Veo providers would go here (lazy import, cost model).
    return KenBurnsFallback(settings)
