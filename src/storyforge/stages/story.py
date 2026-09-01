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
from storyforge.core.types import GroundingLevel, Story, StoryConfig, StoryScene
from storyforge.kb.compiler import BriefCompiler
from storyforge.lint import LintIssue, LintReport, lint_scene
from storyforge.providers.knowledge import build_universe_store
from storyforge.textnorm import TextNormalizer

logger = get_logger(__name__)


def _lint_feedback_text(issues: list[LintIssue]) -> str:
    """One-line-per-issue feedback string embedded in the retry prompt."""
    lines = [f"- [{i.severity}] {i.rule}: {i.suggestion or i.excerpt}" for i in issues]
    return "\n".join(lines)


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
        normalizer = TextNormalizer()
        prompt_rows: list[dict[str, Any]] = []
        lint_report = LintReport()
        retry_prompts: list[dict[str, object]] = []
        quality_flags: set[str] = set()
        try:
            outline, outline_prompt = writer.generate_outline(self.config, brief)
            prompt_rows.append({"section": "outline", "prompt": outline_prompt})
            scenes: list[StoryScene] = []
            for beat in outline:
                brief = compiler.update(brief, beat)
                scene, scene_prompt = writer.generate_scene(self.config, beat, brief)
                # M2-D2 §2.3: lint after drafting; regenerate once on fail.
                issues = lint_scene(scene, normalizer)
                lint_report.issues.extend(issues)
                fails = [i for i in issues if i.severity == "fail"]
                if fails:
                    retried_scene, retried_prompt = writer.generate_scene(
                        self.config,
                        beat,
                        brief,
                        lint_feedback=_lint_feedback_text(fails),
                    )
                    retry_prompts.append(
                        {
                            "scene": scene.scene_id,
                            "prompt": retried_prompt,
                            "feedback": _lint_feedback_text(fails),
                        }
                    )
                    issues_after = lint_scene(retried_scene, normalizer)
                    lint_report.issues.extend(issues_after)
                    if any(i.severity == "fail" for i in issues_after):
                        # Still failing → flag, don't block (loose).
                        quality_flags.add(scene.scene_id)
                        logger.warning(
                            "scene still fails lint after retry",
                            scene=scene.scene_id,
                        )
                    else:
                        scene = retried_scene
                scenes.append(scene)
                prompt_rows.append({"section": f"scene:{beat.beat_id}", "prompt": scene_prompt})
        except StoryForgeError:
            raise
        except Exception as exc:
            raise StoryGenerationError(
                "story generation failed", details={"error": str(exc)}
            ) from exc

        # Block on persistent lint failures only in strict mode.
        if quality_flags and self.config.grounding is GroundingLevel.STRICT:
            raise StoryGenerationError(
                "story failed lint in strict mode",
                details={"scenes": sorted(quality_flags)},
            )

        story = Story(config=self.config, outline=outline, scenes=scenes)
        ctx.store.write_model(story_path, story)
        ctx.store.write_jsonl(ctx.store.dir("04_story") / "prompts.jsonl", prompt_rows)
        ctx.store.write_model(ctx.store.dir("04_story") / "lint_report.json", lint_report)
        ctx.store.write_jsonl(
            ctx.store.dir("04_story") / "prompts_used" / "retry_scenes.jsonl", retry_prompts
        )
        logger.info("story generated", scenes=len(scenes), title=self.config.title)
        return story
