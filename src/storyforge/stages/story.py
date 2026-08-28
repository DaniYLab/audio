"""Stage 4 — generate the story from config + knowledge base (2-pass brief).

Pass 1 (before outline): ``BriefCompiler.build`` -> theme + entity dossiers.
Pass 2 (per beat): ``BriefCompiler.update`` -> scene palette, citation-deduped.
The writer receives the rendered brief sections, never raw hit strings.
"""

from __future__ import annotations

from typing import Any

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import StoryForgeError, StoryGenerationError
from storyforge.core.logging import get_logger
from storyforge.core.types import Story, StoryConfig, StoryScene
from storyforge.kb.compiler import BriefCompiler
from storyforge.providers.knowledge import build_universe_store

logger = get_logger(__name__)


class StoryStage(Stage):
    name = "story"

    def __init__(self, config: StoryConfig) -> None:
        self.config = config

    def run(self, ctx: StageContext, *, force: bool = False) -> Story:
        from storyforge.providers.llm import build_writer

        story_path = ctx.store.story_path()
        if story_path.exists() and not force:
            logger.info("story exists, skipping")
            return ctx.store.read_model(story_path, Story)

        # Persist the resolved config for provenance.
        ctx.store.write_model(ctx.store.dir("04_story") / "config.json", self.config)

        store = build_universe_store(ctx.settings, self.config.universe)
        compiler = BriefCompiler(store, self.config)
        brief = compiler.build()

        writer = build_writer(ctx.settings)
        prompt_rows: list[dict[str, Any]] = []
        try:
            outline, outline_prompt = writer.generate_outline(self.config, brief)
            prompt_rows.append({"section": "outline", "prompt": outline_prompt})
            scenes: list[StoryScene] = []
            for beat in outline:
                brief = compiler.update(brief, beat)
                scene, scene_prompt = writer.generate_scene(self.config, beat, brief)
                scenes.append(scene)
                prompt_rows.append({"section": f"scene:{beat.beat_id}", "prompt": scene_prompt})
        except StoryForgeError:
            raise
        except Exception as exc:
            raise StoryGenerationError(
                "story generation failed", details={"error": str(exc)}
            ) from exc

        story = Story(config=self.config, outline=outline, scenes=scenes)
        ctx.store.write_model(story_path, story)
        ctx.store.write_jsonl(ctx.store.dir("04_story") / "prompts.jsonl", prompt_rows)
        logger.info("story generated", scenes=len(scenes), title=self.config.title)
        return story
