"""Stage 7 — assemble the final video with FFmpeg.

Sync model (see ARCHITECTURE.md): each scene = its narration clip duration;
the illustration is shown for exactly that long (Ken Burns zoom via zoompan),
clips are concatenated, and subtitles are burned from generated SRT timings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import VideoAssemblyError
from storyforge.core.logging import get_logger
from storyforge.core.types import (
    Illustration,
    NarrationClip,
    SubtitleLine,
    VideoResult,
)

if TYPE_CHECKING:
    from storyforge.recap import RecapSegment

logger = get_logger(__name__)

# M4-A5: CC0 music library root (BA owns the files + LICENSES.md).
MUSIC_DIR = Path("assets/music_cc0")


class VideoStage(Stage):
    name = "video"

    def __init__(
        self,
        clips: list[NarrationClip],
        illustrations: list[Illustration],
        music_mood: str | None = None,
        recap: RecapSegment | None = None,
    ) -> None:
        self.clips = clips
        self.illustrations = {i.scene_id: i for i in illustrations}
        self.music_mood = music_mood
        # M4-A2: optional Previously-On recap clip prepended before scene 0.
        self.recap = recap

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

        # M4-A2: prepend recap clip if provided.
        recap_clips: list[NarrationClip] = []
        recap_illustrations: dict[str, Illustration] = {}
        recap_texts: dict[str, str] = {}
        if self.recap is not None:
            from storyforge.core.types import NarrationClip as NCli
            from storyforge.providers.tts import probe_duration

            rec_dur = probe_duration(self.recap.audio_path, settings.ffprobe_bin)
            recap_clips = [
                NCli(
                    scene_id="recap",
                    audio_path=Path(self.recap.audio_path),
                    duration_seconds=rec_dur,
                    char_count=len(self.recap.subtitle),
                )
            ]
            recap_illustrations = {
                "recap": Illustration(
                    scene_id="recap", image_path=Path(self.recap.image_path), prompt_hash="recap"
                )
            }
            recap_texts["recap"] = self.recap.subtitle

        all_clips = recap_clips + self.clips
        all_illustrations = {**recap_illustrations, **self.illustrations}
        # The "recap" scene is not in the story artifact, so its subtitle text
        # is injected explicitly before building the SRT.
        scene_texts = {**recap_texts, **self._scene_texts(ctx)}

        subtitles = self._build_subtitles(ctx, clips=all_clips, scene_texts=scene_texts)
        srt_path = ctx.store.dir("logs") / "subtitles.srt"
        srt_path.write_text(self._to_srt(subtitles), encoding="utf-8")

        segment_paths = [
            self._render_segment(ctx, clip, illust=all_illustrations.get(clip.scene_id))
            for clip in all_clips
            if clip.scene_id in all_illustrations
        ]
        if len(segment_paths) != len(all_clips):
            missing = {c.scene_id for c in all_clips} - set(all_illustrations)
            raise VideoAssemblyError(f"missing illustrations for scenes: {sorted(missing)}")

        concat_list = ctx.store.dir("logs") / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in segment_paths), encoding="utf-8"
        )

        music_path = self._resolve_music(ctx)
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
            _audio_concat_arg(all_clips),
        ]
        if music_path is not None:
            cmd += ["-i", str(music_path)]

        vf_arg: str | None = None
        if settings.burn_subtitles:
            vf_arg = f"subtitles={srt_path.as_posix()}"
        af_arg = self._audio_filter(ctx, music_path)
        if settings.burn_subtitles:
            cmd += ["-vf", vf_arg]  # type: ignore[list-item]
        if af_arg:
            cmd += ["-af", af_arg]
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
        import time

        render_start = time.monotonic()
        self._run_ffmpeg(cmd, log_path)
        render_seconds = round(time.monotonic() - render_start, 3)

        total = sum(c.duration_seconds for c in all_clips)
        # T1-DEV1: video has no dollar cost but the run records its render
        # wall-clock time (for the cost report / ops dashboards).
        from storyforge.core.metrics import current_run_recorder

        recorder = current_run_recorder()
        if recorder is not None:
            recorder.record("video", render_seconds=render_seconds)
        logger.info("video assembled", path=str(out_path), seconds=total)
        return VideoResult(
            video_path=out_path,
            duration_seconds=total,
            scene_count=len(all_clips),
            ffmpeg_command_log=log_path,
        )

    def _render_segment(
        self, ctx: StageContext, clip: NarrationClip, illust: Illustration | None = None
    ) -> Path:
        """Render one scene: still image + Ken Burns, duration = clip duration."""
        settings = ctx.settings.video
        illustration = illust or self.illustrations[clip.scene_id]
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

    def _build_subtitles(
        self,
        ctx: StageContext,
        clips: list[NarrationClip] | None = None,
        scene_texts: dict[str, str] | None = None,
    ) -> list[SubtitleLine]:
        # Scene-level subtitles from clip timings; text comes from the story
        # artifact so subtitles always match the narration exactly.
        scenes = scene_texts or self._scene_texts(ctx)
        use_clips = clips if clips is not None else self.clips
        lines: list[SubtitleLine] = []
        cursor = 0.0
        for i, clip in enumerate(use_clips):
            lines.append(
                SubtitleLine(
                    index=i + 1,
                    start=cursor,
                    end=cursor + clip.duration_seconds,
                    text=scenes.get(clip.scene_id, ""),
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

    def _resolve_music(self, ctx: StageContext) -> Path | None:
        """T3-DEV1: resolve the CC0 music file via config/music_moods.yaml."""
        if not self.music_mood:
            return None
        from storyforge.music import resolve_mood_file

        return resolve_mood_file(self.music_mood)

    def _audio_filter(self, ctx: StageContext, music_path: Path | None) -> str | None:
        """M3-W5 §9.1: FFmpeg filtergraph for background music mixing."""
        if music_path is None:
            return None
        return (
            "[1:a]aloop=loop=-1:size=2e+09,"
            "afade=t=in:st=0:d=2,volume=0.15[bgm];"
            "[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )


def _audio_concat_arg(clips: list[NarrationClip]) -> str:
    """Concat protocol argument for the narration track: 'concat:a|b|c'."""
    return "concat:" + "|".join(str(c.audio_path.as_posix()) for c in clips)
