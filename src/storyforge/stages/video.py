"""Stage 7 — assemble the final video with FFmpeg.

Sync model (see ARCHITECTURE.md): each scene = its narration clip duration;
the illustration is shown for exactly that long (Ken Burns zoom via zoompan),
clips are concatenated, and subtitles are burned from generated SRT timings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import VideoAssemblyError
from storyforge.core.logging import get_logger
from storyforge.core.types import (
    Illustration,
    NarrationClip,
    SubtitleLine,
    VideoResult,
)

logger = get_logger(__name__)


class VideoStage(Stage):
    name = "video"

    def __init__(self, clips: list[NarrationClip], illustrations: list[Illustration]) -> None:
        self.clips = clips
        self.illustrations = {i.scene_id: i for i in illustrations}

    def run(self, ctx: StageContext, *, force: bool = False) -> VideoResult:
        settings = ctx.settings.video
        out_path = ctx.store.video_path()
        if out_path.exists() and not force:
            return VideoResult(
                video_path=out_path,
                duration_seconds=sum(c.duration_seconds for c in self.clips),
                scene_count=len(self.clips),
                ffmpeg_command_log=ctx.store.dir("logs") / "ffmpeg.log",
            )

        if not self.clips:
            raise VideoAssemblyError("no narration clips to assemble")

        subtitles = self._build_subtitles(ctx)
        srt_path = ctx.store.dir("logs") / "subtitles.srt"
        srt_path.write_text(self._to_srt(subtitles), encoding="utf-8")

        segment_paths = [
            self._render_segment(ctx, clip)
            for clip in self.clips
            if clip.scene_id in self.illustrations
        ]
        if len(segment_paths) != len(self.clips):
            missing = {c.scene_id for c in self.clips} - set(self.illustrations)
            raise VideoAssemblyError(f"missing illustrations for scenes: {sorted(missing)}")

        concat_list = ctx.store.dir("logs") / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in segment_paths), encoding="utf-8"
        )

        cmd = [
            settings.ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-i",
            _audio_concat_arg(self.clips),
        ]
        if settings.burn_subtitles:
            cmd += ["-vf", f"subtitles={srt_path.as_posix()}"]
        cmd += [
            "-c:v",
            "libx264",
            "-crf",
            str(settings.crf),
            "-preset",
            settings.preset,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out_path),
        ]

        log_path = ctx.store.dir("logs") / "ffmpeg.log"
        self._run_ffmpeg(cmd, log_path)

        total = sum(c.duration_seconds for c in self.clips)
        logger.info("video assembled", path=str(out_path), seconds=total)
        return VideoResult(
            video_path=out_path,
            duration_seconds=total,
            scene_count=len(self.clips),
            ffmpeg_command_log=log_path,
        )

    def _render_segment(self, ctx: StageContext, clip: NarrationClip) -> Path:
        """Render one scene: still image + Ken Burns, duration = clip duration."""
        settings = ctx.settings.video
        illustration = self.illustrations[clip.scene_id]
        out = ctx.store.dir("logs") / f"seg_{clip.scene_id}.mp4"
        frames = int(clip.duration_seconds * 30) + 1  # 30 fps

        vf = f"scale={ctx.settings.imaging.width}:{ctx.settings.imaging.height}"
        if settings.ken_burns:
            # Slow zoom-in; z increases linearly from 1.0 to 1.1 over the clip.
            vf = (
                f"zoompan=z='1+0.1*on/{frames}':d={frames}"
                f":s={ctx.settings.imaging.width}x{ctx.settings.imaging.height}:fps=30"
            )

        cmd = [
            settings.ffmpeg_bin,
            "-y",
            "-loop",
            "1",
            "-i",
            str(illustration.image_path),
            "-i",
            str(clip.audio_path),
            "-vf",
            vf,
            "-t",
            str(clip.duration_seconds),
            "-c:v",
            "libx264",
            "-crf",
            str(settings.crf),
            "-preset",
            settings.preset,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
        self._run_ffmpeg(cmd, ctx.store.dir("logs") / f"ffmpeg_{clip.scene_id}.log")
        return out

    def _build_subtitles(self, ctx: StageContext) -> list[SubtitleLine]:
        # Scene-level subtitles from clip timings; text comes from the story
        # artifact so subtitles always match the narration exactly.
        lines: list[SubtitleLine] = []
        cursor = 0.0
        scene_texts = self._scene_texts(ctx)
        for i, clip in enumerate(self.clips):
            lines.append(
                SubtitleLine(
                    index=i + 1,
                    start=cursor,
                    end=cursor + clip.duration_seconds,
                    text=scene_texts.get(clip.scene_id, ""),
                )
            )
            cursor += clip.duration_seconds
        return lines

    def _scene_texts(self, ctx: StageContext) -> dict[str, str]:
        from storyforge.core.types import Story

        story = ctx.store.read_model(ctx.store.story_path(), Story)
        return {scene.scene_id: scene.narration_text for scene in story.scenes}

    @staticmethod
    def _to_srt(lines: list[SubtitleLine]) -> str:
        def fmt(seconds: float) -> str:
            h, rem = divmod(int(seconds), 3600)
            m, s = divmod(rem, 60)
            ms = int((seconds - int(seconds)) * 1000)
            return f"{h:02}:{m:02}:{s:02},{ms:03}"

        blocks = [
            f"{line.index}\n{fmt(line.start)} --> {fmt(line.end)}\n{line.text}\n" for line in lines
        ]
        return "\n".join(blocks)

    def _run_ffmpeg(self, cmd: list[str], log_path: Path) -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
        except subprocess.TimeoutExpired as exc:
            raise VideoAssemblyError("ffmpeg timed out") from exc
        log_path.write_text(result.stderr or "", encoding="utf-8")
        if result.returncode != 0:
            tail = (result.stderr or "")[-2000:]
            raise VideoAssemblyError(
                "ffmpeg failed", details={"returncode": result.returncode, "stderr_tail": tail}
            )


def _audio_concat_arg(clips: list[NarrationClip]) -> str:
    """Concat protocol argument for the narration track: 'concat:a|b|c'."""
    return "concat:" + "|".join(str(c.audio_path.as_posix()) for c in clips)
