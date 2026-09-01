"""Stage 4 — generate the story from config + knowledge base (2-pass brief).

Pass 1 (before outline): ``BriefCompiler.build`` -> theme + entity dossiers.
Pass 2 (per beat): ``BriefCompiler.update`` -> scene palette, citation-deduped.
The writer receives the rendered brief sections, never raw hit strings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import StoryForgeError, StoryGenerationError
from storyforge.core.logging import get_logger
from storyforge.core.types import GroundingLevel, Story, StoryConfig, StoryScene
from storyforge.guard import CheckpointDeltaGuard, GuardError
from storyforge.kb.compiler import BriefCompiler
from storyforge.lint import LintIssue, LintReport, lint_scene
from storyforge.providers.knowledge import build_universe_store
from storyforge.stylestat import StyleStatsTracker
from storyforge.textnorm import TextNormalizer

if TYPE_CHECKING:
    from storyforge.ledger.store import LedgerStore

logger = get_logger(__name__)


def _lint_feedback_text(issues: list[LintIssue]) -> str:
    """One-line-per-issue feedback string embedded in the retry prompt."""
    lines = [f"- [{i.severity}] {i.rule}: {i.suggestion or i.excerpt}" for i in issues]
    return "\n".join(lines)


def _guard_feedback_text(exc: GuardError) -> str:
    return f"[ARTIFACT GUARD] {exc!s} — write something different"


class StoryStage(Stage):
    name = "story"

    def __init__(self, config: StoryConfig) -> None:
        self.config = config

    def _build_ledger(self, ctx: StageContext) -> LedgerStore | None:
        """Inject the universe ledger (M3-W1); None when no ledger exists yet."""
        from storyforge.ledger.store import build_ledger

        universe_dir = ctx.settings.knowledge.ledgers_dir / self.config.universe
        if not universe_dir.exists():
            return None
        return build_ledger(universe_dir)

    def run(self, ctx: StageContext, *, force: bool = False) -> Story:
        from storyforge.providers.llm import build_writer

        story_path = ctx.store.story_path()
        if story_path.exists() and not force:
            logger.info("story exists, skipping")
            return ctx.store.read_model(story_path, Story)

        # Persist the resolved config for provenance.
        ctx.store.write_model(ctx.store.dir("04_story") / "config.json", self.config)

        store = build_universe_store(ctx.settings, self.config.universe)
        ledger = self._build_ledger(ctx)
        compiler = BriefCompiler(store, self.config, ledger=ledger)
        brief = compiler.build()

        writer = build_writer(ctx.settings)
        normalizer = TextNormalizer()
        prompt_rows: list[dict[str, Any]] = []
        lint_report = LintReport()
        retry_prompts: list[dict[str, object]] = []
        quality_flags: set[str] = set()
        track_style = ctx.settings.story.style_stats
        stylestat = StyleStatsTracker() if track_style else None
        guard = CheckpointDeltaGuard()

        # Build baseline digests from a previously existing story (resume).
        if story_path.exists():
            try:
                existing = Story.model_validate_json(story_path.read_text(encoding="utf-8"))
                for scene in existing.scenes:
                    guard.add_baseline(
                        CheckpointDeltaGuard.digest(scene.narration_text, scene.image_prompt)
                    )
            except Exception:
                pass  # best-effort — fresh run may have a stale artifact

        try:
            outline, outline_prompt = writer.generate_outline(self.config, brief)
            prompt_rows.append({"section": "outline", "prompt": outline_prompt})
            scenes: list[StoryScene] = []
            for beat in outline:
                brief = compiler.update(brief, beat)

                def _retry_feedback(e: GuardError | None, issues: list[LintIssue]) -> str:
                    parts = []
                    if e is not None:
                        parts.append(_guard_feedback_text(e))
                    if issues:
                        parts.append("LINT: " + _lint_feedback_text(issues))
                    return "\n".join(parts)

                style_stats_text = stylestat.render() if stylestat else None
                scene, scene_prompt = writer.generate_scene(
                    self.config, beat, brief, style_stats=style_stats_text
                )

                # M4-B3: guard check — reject duplicate/unwritten scenes.
                guard_error: GuardError | None = None
                try:
                    guard.check(
                        CheckpointDeltaGuard.digest(scene.narration_text, scene.image_prompt)
                    )
                except GuardError as exc:
                    guard_error = exc
                    logger.warning("guard fail", scene=scene.scene_id, error=str(exc))

                # M2-D2 §2.3: lint after drafting.
                issues = lint_scene(scene, normalizer)
                lint_report.issues.extend(issues)
                fails = [i for i in issues if i.severity == "fail"]

                must_retry = guard_error is not None or fails
                if must_retry:
                    retried_scene, retried_prompt = writer.generate_scene(
                        self.config,
                        beat,
                        brief,
                        lint_feedback=_retry_feedback(guard_error, fails),
                        style_stats=style_stats_text,
                    )
                    retry_prompts.append(
                        {
                            "scene": scene.scene_id,
                            "prompt": retried_prompt,
                            "feedback": _retry_feedback(guard_error, fails),
                        }
                    )
                    # Clear the guard error and re-check the retried scene.
                    guard_error = None
                    try:
                        guard.check(
                            CheckpointDeltaGuard.digest(
                                retried_scene.narration_text, retried_scene.image_prompt
                            )
                        )
                    except GuardError as exc:
                        guard_error = exc
                        logger.warning("guard still fails after retry", scene=scene.scene_id)

                    issues_after = lint_scene(retried_scene, normalizer)
                    lint_report.issues.extend(issues_after)
                    if guard_error is not None or any(i.severity == "fail" for i in issues_after):
                        quality_flags.add(scene.scene_id)
                        logger.warning(
                            "scene still fails after retry",
                            scene=scene.scene_id,
                            guard_error=guard_error is not None,
                        )
                    else:
                        scene = retried_scene

                if stylestat:
                    stylestat.track(scene.narration_text)
                scenes.append(scene)
                prompt_rows.append({"section": f"scene:{beat.beat_id}", "prompt": scene_prompt})
        except StoryForgeError:
            raise
        except Exception as exc:
            raise StoryGenerationError(
                "story generation failed", details={"error": str(exc)}
            ) from exc

        # Block on persistent lint / guard failures only in strict mode.
        if quality_flags and self.config.grounding is GroundingLevel.STRICT:
            raise StoryGenerationError(
                "story failed lint/guard in strict mode",
                details={"scenes": sorted(quality_flags)},
            )

        story = Story(config=self.config, outline=outline, scenes=scenes)
        ctx.store.write_model(story_path, story)
        ctx.store.write_jsonl(ctx.store.dir("04_story") / "prompts.jsonl", prompt_rows)
        ctx.store.write_model(ctx.store.dir("04_story") / "lint_report.json", lint_report)
        ctx.store.write_jsonl(
            ctx.store.dir("04_story") / "prompts_used" / "retry_scenes.jsonl", retry_prompts
        )
        if stylestat:
            stats = stylestat.summarize()
            ctx.store.write_model(ctx.store.dir("04_story") / "style_stats.json", stats)
        logger.info(
            "story generated",
            scenes=len(scenes),
            title=self.config.title,
            style_stats=stylestat is not None,
        )
        return story
