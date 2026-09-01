"""Stage 6 — scene illustrations.

Image prompts are composed centrally here (style + character appearances +
scene hint) so the provider only renders, never improvises character looks.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import ImageGenerationError
from storyforge.core.logging import get_logger
from storyforge.core.types import Illustration, Story, StoryScene

logger = get_logger(__name__)


def _slugify(name: str) -> str:
    """ASCII slug for a character name -> filename-safe ref id (M3-W4 §7.1)."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "character"


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
            # M3-W4: resolve a canonical character reference image, if any.
            reference = self._resolve_reference(ctx, scene)
            try:
                illustration = generator.generate_from_prompt(
                    prompt, str(out_path), reference_image=reference
                )
                illustration.scene_id = scene.scene_id
            except Exception as exc:
                raise ImageGenerationError(
                    f"image generation failed for scene {scene.scene_id}",
                    details={"error": str(exc)},
                ) from exc
            illustrations.append(illustration)
            logger.info("illustration generated", scene=scene.scene_id)
        return illustrations

    def _resolve_reference(self, ctx: StageContext, scene: StoryScene) -> Path | None:
        """Canonical character ref: first character in the beat (M3-W4 §7.1)."""
        if not scene.beat.characters:
            return None
        main = scene.beat.characters[0]
        slug = _slugify(main)
        ref_dir = Path(ctx.settings.knowledge.kb_data_dir) / "characters"
        ref = ref_dir / f"{slug}.png"
        return ref if ref.exists() else None

    @staticmethod
    def _compose_prompt(hint: str, appearances: dict[str, str], art_style: str) -> str:
        parts = [art_style]
        for name, appearance in appearances.items():
            parts.append(f"{name}: {appearance}")
        parts.append(hint)
        return ". ".join(parts)
