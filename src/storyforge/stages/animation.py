"""Stage 6.5 — image-to-video animation (M6-W1).

Wired between imaging and video when an animated-scene provider is configured
(``SF__ANIMATION__PROVIDER=fal_kling|veo``). The default ``kenburns`` config
needs no stage — VideoStage renders Ken Burns internally, so running this
stage in the default configuration would double-render identical motion.

Fallback principle (M6 §1): any provider failure degrades the scene to Ken
Burns (the stage simply does not produce a clip for it, and VideoStage falls
back to its internal image render). Scenes shorter than
``min_duration_seconds`` never call an API provider — they stay Ken Burns.

Output: one motion clip per scene in ``06_animation/<scene_id>.mp4``. The
stage returns ``{scene_id: AnimatedClip}``; VideoStage muxes the narration
onto the clip instead of rendering a Ken Burns segment.
"""

from __future__ import annotations

from storyforge.core.config import Settings
from storyforge.core.contracts import Stage, StageContext
from storyforge.core.logging import get_logger
from storyforge.core.types import Illustration, NarrationClip
from storyforge.providers.animation import (
    AnimatedClip,
    KenBurnsFallback,
    MotionSpec,
    build_animation_provider,
)

logger = get_logger(__name__)


def _motion_for(
    settings: Settings, duration_seconds: float, prompt: str | None = None
) -> MotionSpec:
    """Motion for a scene: API-worthy scenes use the configured default motion;
    very short scenes use Ken Burns (cheap, no API call needed)."""
    kind = settings.animation.motion_default
    if duration_seconds < settings.animation.min_duration_seconds:
        kind = "kenburns"
    return MotionSpec(kind=kind, intensity=0.3, prompt=prompt)


def _scene_prompts(ctx: StageContext) -> dict[str, str]:
    """scene_id -> image_prompt from the story artifact (text-guided providers)."""
    try:
        import json

        data = json.loads(ctx.store.story_path().read_text(encoding="utf-8"))
        return {s["scene_id"]: s.get("image_prompt", "") for s in data.get("scenes", [])}
    except Exception:
        return {}


class AnimationStage(Stage):
    name = "animation"

    def __init__(
        self,
        clips: list[NarrationClip],
        illustrations: dict[str, Illustration],
    ) -> None:
        self.clips = clips
        self.illustrations = illustrations

    def run(self, ctx: StageContext, *, force: bool = False) -> dict[str, AnimatedClip]:
        provider = build_animation_provider(ctx.settings)
        out_dir = ctx.store.dir("06_animation")
        out_dir.mkdir(parents=True, exist_ok=True)

        animated: dict[str, AnimatedClip] = {}
        api_calls = 0
        kenburns = 0
        uses_api = not isinstance(provider, KenBurnsFallback)
        prompts = _scene_prompts(ctx)

        for clip in self.clips:
            illustration = self.illustrations.get(clip.scene_id)
            if illustration is None:
                logger.warning("animation skipped — no illustration", scene=clip.scene_id)
                continue
            out_path = out_dir / f"{clip.scene_id}.mp4"
            if out_path.exists() and not force:
                # Resume: trust the on-disk clip.
                kenburns += 1
                animated[clip.scene_id] = AnimatedClip(
                    clip_path=out_path,
                    duration_seconds=clip.duration_seconds,
                    provider="kenburns_fallback",
                    cost_usd=0.0,
                )
                continue

            motion = _motion_for(
                ctx.settings, clip.duration_seconds, prompt=prompts.get(clip.scene_id)
            )
            # Scenes under min_duration never hit the API (M6-W1 §2.1).
            if motion.kind == "kenburns" and uses_api:
                logger.info("animation kenburns (short scene)", scene=clip.scene_id)
                kenburns += 1
                continue
            try:
                result = provider.animate(
                    str(illustration.image_path),
                    clip.duration_seconds,
                    motion,
                    str(out_path),
                )
            except Exception as exc:
                logger.warning(
                    "animation failed — ken burns fallback",
                    scene=clip.scene_id,
                    error=str(exc),
                )
                kenburns += 1
                continue

            if result.provider == "kenburns_fallback":
                kenburns += 1
            else:
                api_calls += 1
            animated[clip.scene_id] = result

        from storyforge.core.metrics import current_run_recorder

        recorder = current_run_recorder()
        if recorder is not None:
            recorder.record(
                "animation",
                scenes_animated=api_calls,
                scenes_kenburns=kenburns,
                animation_cost_usd=sum(c.cost_usd for c in animated.values()),
            )
        logger.info(
            "animation done",
            animated=api_calls,
            kenburns=kenburns,
            total=len(self.clips),
        )
        return animated
