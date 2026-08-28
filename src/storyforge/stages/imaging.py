"""Stage 6 — scene illustrations.

Image prompts are composed centrally here (style + character appearances +
scene hint) so the provider only renders, never improvises character looks.
"""

from __future__ import annotations

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import ImageGenerationError
from storyforge.core.logging import get_logger
from storyforge.core.types import Illustration, Story

logger = get_logger(__name__)


class ImagingStage(Stage):
    name = "imaging"

    def __init__(self, story: Story) -> None:
        self.story = story

    def run(self, ctx: StageContext, *, force: bool = False) -> list[Illustration]:
        from storyforge.providers.imaging import build_image_generator

        generator = build_image_generator(ctx.settings)
        style = self.story.config.style
        appearances = {c.name: c.appearance for c in self.story.config.characters}

        illustrations: list[Illustration] = []
        for scene in self.story.scenes:
            out_path = ctx.store.dir("06_images") / f"{scene.scene_id}.png"
            if out_path.exists() and not force:
                illustrations.append(
                    Illustration(
                        scene_id=scene.scene_id,
                        image_path=out_path,
                        prompt_hash=scene.image_prompt[:64],
                    )
                )
                continue

            prompt = self._compose_prompt(
                scene.beat.image_hint or scene.image_prompt, appearances, style.art_style
            )
            try:
                illustration = generator.generate_from_prompt(prompt, str(out_path))
                illustration.scene_id = scene.scene_id
            except Exception as exc:
                raise ImageGenerationError(
                    f"image generation failed for scene {scene.scene_id}",
                    details={"error": str(exc)},
                ) from exc
            illustrations.append(illustration)
            logger.info("illustration generated", scene=scene.scene_id)
        return illustrations

    @staticmethod
    def _compose_prompt(hint: str, appearances: dict[str, str], art_style: str) -> str:
        parts = [art_style]
        for name, appearance in appearances.items():
            parts.append(f"{name}: {appearance}")
        parts.append(hint)
        return ". ".join(parts)
