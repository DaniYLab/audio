"""Stage 5 — text-to-speech narration, one clip per scene.

Per-scene synthesis (not whole-story) is what makes image/audio sync trivial:
the video stage simply reads each clip's duration via ffprobe.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import TTSError
from storyforge.core.logging import get_logger
from storyforge.core.types import NarrationClip, Story
from storyforge.textnorm import TextNormalizer

logger = get_logger(__name__)


class TTSStage(Stage):
    name = "tts"

    def __init__(self, story: Story) -> None:
        self.story = story

    def _cache_key(self, engine: str, voice: str, text: str) -> str:
        """M3-W3: sha256 hash of (engine + voice + normalized_text)."""
        raw = f"{engine}::{voice}::{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, cache_dir: Path, key: str) -> Path:
        return cache_dir / f"{key}.mp3"

    def run(self, ctx: StageContext, *, force: bool = False) -> list[NarrationClip]:
        from storyforge.providers.tts import build_tts, probe_duration

        tts = build_tts(ctx.settings)
        default_voice = ctx.settings.tts.edge_voice
        voice_by_character = {
            c.name: c.tts_voice for c in self.story.config.characters if c.tts_voice
        }
        normalizer = TextNormalizer() if ctx.settings.tts.normalize_text else None
        cache_dir = ctx.settings.tts.cache_dir
        engine = ctx.settings.tts.engine

        clips: list[NarrationClip] = []
        cache_hits = 0
        for scene in self.story.scenes:
            narration = scene.narration_text
            normalized_text: str | None = None
            if normalizer is not None:
                result = normalizer.normalize(scene.narration_text)
                narration = result.normalized
                normalized_text = result.normalized

            out_path = ctx.store.dir("05_tts") / f"{scene.scene_id}.mp3"

            # If the scene features a single named character, use their voice.
            voice = default_voice
            if len(scene.beat.characters) == 1:
                voice = voice_by_character.get(scene.beat.characters[0], default_voice)

            # M3-W3: TTS audio cache — skip the API on a cache hit.
            cache_key = self._cache_key(engine, voice or "", narration)
            cache_path = self._cache_path(cache_dir, cache_key)
            if cache_path.exists() and not force:
                cache_path.replace(out_path)
                cache_hits += 1
                duration = probe_duration(str(out_path), ctx.settings.video.ffprobe_bin)
                clip = NarrationClip(
                    scene_id=scene.scene_id,
                    audio_path=out_path,
                    duration_seconds=duration,
                    char_count=len(narration),
                    normalized_text=normalized_text,
                )
                clips.append(clip)
                logger.info("tts cache hit", scene=scene.scene_id, key=cache_key[:12])
                continue

            if out_path.exists() and not force:
                duration = probe_duration(str(out_path), ctx.settings.video.ffprobe_bin)
                clips.append(
                    NarrationClip(
                        scene_id=scene.scene_id,
                        audio_path=out_path,
                        duration_seconds=duration,
                        char_count=len(narration),
                        normalized_text=normalized_text,
                    )
                )
                continue

            synth_scene = (
                scene.model_copy(update={"narration_text": narration})
                if narration != scene.narration_text
                else scene
            )
            try:
                clip = tts.synthesize(synth_scene, str(out_path), voice)
            except Exception as exc:
                raise TTSError(
                    f"synthesis failed for scene {scene.scene_id}",
                    details={"error": str(exc)},
                ) from exc
            clip.normalized_text = normalized_text
            # Write to cache for future runs.
            cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                out_path.replace(cache_path)
            except OSError:
                pass  # cache write is best-effort
            clips.append(clip)
            logger.info(
                "narration synthesized",
                scene=scene.scene_id,
                seconds=clip.duration_seconds,
            )

        logger.info("tts done", cache_hits=cache_hits, synthesized=len(clips) - cache_hits)
        return clips
