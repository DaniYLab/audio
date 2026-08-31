"""Stage 5 — text-to-speech narration, one clip per scene.

Per-scene synthesis (not whole-story) is what makes image/audio sync trivial:
the video stage simply reads each clip's duration via ffprobe.
"""

from __future__ import annotations

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import TTSError
from storyforge.core.logging import get_logger
from storyforge.core.types import NarrationClip, Story
from storyforge.normalize import TextNormalizer

logger = get_logger(__name__)


class TTSStage(Stage):
    name = "tts"

    def __init__(self, story: Story) -> None:
        self.story = story

    def run(self, ctx: StageContext, *, force: bool = False) -> list[NarrationClip]:
        from storyforge.providers.tts import build_tts

        tts = build_tts(ctx.settings)
        default_voice = ctx.settings.tts.edge_voice
        voice_by_character = {
            c.name: c.tts_voice for c in self.story.config.characters if c.tts_voice
        }
        normalizer = TextNormalizer() if ctx.settings.tts.normalize_text else None

        clips: list[NarrationClip] = []
        for scene in self.story.scenes:
            narration = (
                normalizer.normalize(scene.narration_text)
                if normalizer is not None
                else scene.narration_text
            )
            out_path = ctx.store.dir("05_tts") / f"{scene.scene_id}.mp3"
            if out_path.exists() and not force:
                from storyforge.providers.tts import probe_duration

                clips.append(
                    NarrationClip(
                        scene_id=scene.scene_id,
                        audio_path=out_path,
                        duration_seconds=probe_duration(
                            str(out_path), ctx.settings.video.ffprobe_bin
                        ),
                        char_count=len(narration),
                    )
                )
                continue

            # If the scene features a single named character, use their voice.
            voice = default_voice
            if len(scene.beat.characters) == 1:
                voice = voice_by_character.get(scene.beat.characters[0], default_voice)

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
            clips.append(clip)
            logger.info(
                "narration synthesized", scene=scene.scene_id, seconds=clip.duration_seconds
            )

        return clips
