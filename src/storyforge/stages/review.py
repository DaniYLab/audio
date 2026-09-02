"""Reviewer pass (M3-V2) — continuity gate between story and tts.

Flow (m3_design.md §2.1):

    story.json ──► [extract facts: 1 LLM call] ──► [find_conflicts per fact,
                                                    rule-based ledger] ──► review.json

Verdict handling:
- NO_CONFLICT → the fact joins ``facts_to_record`` (recorded when the
  episode ships — by the batch finisher, never inside this stage).
- CONFLICT → strict: the stage fails fast with the report attached (scene
  auto-regeneration lands with Dev 2's writer integration, M3-W1/W2);
  loose: warning + ``needs_review``, pipeline continues.
- CONFLICT inside a beat declared ``intent="twist"`` → TWIST_OK (intentional
  character development, not hallucination).

Cost: exactly 1 LLM call per episode (extraction); conflict checking is
pure in-memory ledger logic.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from storyforge.core.config import Settings
from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import StoryForgeError

if TYPE_CHECKING:
    from storyforge.ledger.store import LedgerStore
from storyforge.core.logging import get_logger
from storyforge.core.types import GroundingLevel, Story, StoryScene
from storyforge.kb.types import (
    ConflictReport,
    ConflictVerdict,
    Fact,
    FactKind,
    FactOrigin,
)

logger = get_logger(__name__)


class ReviewError(StoryForgeError):
    """Reviewer found an unintentional contradiction in strict mode."""


class ReviewSummary(BaseModel):
    n_new_facts: int = 0
    n_conflict: int = 0
    n_twist: int = 0


class ReviewArtifact(BaseModel):
    """``04_story/review.json`` — reports + aggregates for the operator."""

    universe: str
    reports: list[ConflictReport] = Field(default_factory=list)
    facts_to_record: list[Fact] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)  # fact_ids in conflict (loose)
    summary: ReviewSummary = Field(default_factory=ReviewSummary)


Extractor = Callable[[Story], list[Fact]]


def llm_extractor(settings: Settings) -> Extractor:
    """Default extractor: writer model + prompts/review_extract.txt, 1 call."""

    def extract(story: Story) -> list[Fact]:
        from storyforge.providers.llm import LLMClient, fill_prompt, load_prompt

        client = LLMClient(settings, settings.llm.writer_model)
        template = load_prompt("review_extract")
        scenes_text = "\n\n".join(
            f"SCENE {scene.scene_id} (beat {scene.beat.beat_id}"
            + (", intent=twist" if scene.beat.intent == "twist" else "")
            + ")\n"
            + scene.narration_text
            + ("\nfacts_used: " + ", ".join(scene.facts_used) if scene.facts_used else "")
            for scene in story.scenes
        )
        response = client.chat(
            system=fill_prompt(template, {"language": story.config.language}),
            user=fill_prompt(template, {"scenes": scenes_text[:24000]}),
        )
        return parse_fact_lines(response)

    return extract


def parse_fact_lines(response: str) -> list[Fact]:
    """Parse 'FACT: <kind> | <subject> | <statement> | <origin> | <chunk_refs>'."""
    facts: list[Fact] = []
    for line in response.splitlines():
        line = line.strip()
        if not line.upper().startswith("FACT:"):
            continue
        parts = [p.strip() for p in line[len("FACT:") :].split("|")]
        if len(parts) < 3 or not parts[2]:
            continue
        try:
            kind = FactKind(parts[0].lower())
        except ValueError:
            continue
        origin = FactOrigin.INFERRED
        if len(parts) > 3 and parts[3]:
            try:
                origin = FactOrigin(parts[3].lower())
            except ValueError:
                origin = FactOrigin.INFERRED
        chunk_refs: list[str] = []
        if len(parts) > 4 and parts[4].strip() not in ("", "-"):
            chunk_refs = [c.strip() for c in parts[4].split(",") if c.strip()]
        if origin is FactOrigin.CITED and not chunk_refs:
            origin = FactOrigin.INFERRED  # cited without refs is invalid
        facts.append(
            Fact(
                fact_id=f"f_ext{len(facts):04d}",
                kind=kind,
                subject=parts[1],
                statement=parts[2],
                origin=origin,
                chunk_refs=chunk_refs,
                episode_id="pending",  # stamped when the episode ships
                extracted_by="llm",
            )
        )
    return facts


class ReviewStage(Stage):
    name = "review"

    def __init__(
        self,
        story: Story,
        *,
        extractor: Extractor | None = None,
        ledger: LedgerStore | None = None,
    ) -> None:
        self.story = story
        self._extractor = extractor
        self._ledger = ledger

    def run(self, ctx: StageContext, *, force: bool = False) -> ReviewArtifact:
        out_path = ctx.store.dir("04_story") / "review.json"
        if out_path.exists() and not force:
            logger.info("review exists, skipping")
            return ReviewArtifact.model_validate_json(out_path.read_text(encoding="utf-8"))

        # M4-B3 AC4: the reviewer must produce a NEW on-disk artifact. The
        # guard rejects an unchanged/duplicate review.json (e.g. the extractor
        # silently returned the same facts).
        from storyforge.guard import CheckpointDeltaGuard

        guard = CheckpointDeltaGuard()
        if out_path.exists():
            guard.add_baseline(guard.digest(out_path.read_text(encoding="utf-8"), "review.json"))

        extractor = self._extractor or llm_extractor(ctx.settings)
        ledger = self._ledger or self._build_ledger(ctx)
        strict = self.story.config.grounding is GroundingLevel.STRICT

        facts = extractor(self.story)
        reports: list[ConflictReport] = []
        facts_to_record: list[Fact] = []
        needs_review: list[str] = []
        n_twist = 0

        for fact in facts:
            report = ledger.find_conflicts(fact)
            if report.verdict is ConflictVerdict.CONFLICT and self._scene_declares_twist(
                fact.subject
            ):
                report = report.model_copy(update={"verdict": ConflictVerdict.TWIST_OK})
            if report.verdict is ConflictVerdict.NO_CONFLICT:
                reports.append(report)
                facts_to_record.append(fact)
                continue
            if report.verdict is ConflictVerdict.TWIST_OK:
                reports.append(report)
                n_twist += 1
                facts_to_record.append(fact)  # supersede applied at record time
                continue
            # CONFLICT
            if strict:
                # T5-DEV1: auto-regenerate the offending scene ≤ 2 rounds.
                resolved = self._regenerate_conflict(ctx, fact, report, ledger)
                for new_fact in resolved:
                    reports.append(
                        ConflictReport(candidate=new_fact, verdict=ConflictVerdict.NO_CONFLICT)
                    )
                    facts_to_record.append(new_fact)
                continue
            reports.append(report)
            needs_review.append(fact.fact_id)

        artifact = ReviewArtifact(
            universe=self.story.config.universe,
            reports=reports,
            facts_to_record=facts_to_record,
            needs_review=needs_review,
            summary=ReviewSummary(
                n_new_facts=len(facts_to_record),
                n_conflict=len(needs_review),
                n_twist=n_twist,
            ),
        )
        guard.check(guard.digest(artifact.model_dump_json(), "review.json"))
        ctx.store.write_model(out_path, artifact)
        logger.info(
            "review done",
            facts=len(facts),
            conflicts=len(needs_review),
            twists=n_twist,
        )
        return artifact

    def _regenerate_conflict(
        self,
        ctx: StageContext,
        fact: Fact,
        report: ConflictReport,
        ledger: LedgerStore,
    ) -> list[Fact]:
        """T5-DEV1: regenerate the scene that produced a strict conflict.

        Up to 2 rounds of regeneration (spec §5.2). Returns the conflict-free
        facts extracted after the successful round; raises ReviewError when
        the conflict survives both rounds.
        """
        scene = self._find_scene_for_fact(fact)
        if scene is None:
            raise ReviewError(
                f"cannot locate scene for conflict fact {fact.fact_id}",
                details={"fact": fact.statement},
            )

        for attempt in (1, 2):
            scene.narration_text = self._regenerate_scene_narration(ctx, scene, report)
            new_facts = self._extract_scene_facts(ctx, scene)

            clean: list[Fact] = []
            still_conflict: ConflictReport | None = None
            for new_fact in new_facts:
                recheck = ledger.find_conflicts(new_fact)
                if recheck.verdict is ConflictVerdict.CONFLICT:
                    still_conflict = recheck
                    break
                clean.append(new_fact)
            if still_conflict is None:
                return clean  # all re-extracted facts conflict-free
            if attempt == 2:
                raise ReviewError(
                    f"regeneration round {attempt} still conflicts: {still_conflict.reason}",
                    details={
                        "fact": still_conflict.candidate.statement,
                        "conflicts": [f.fact_id for f in still_conflict.conflicts],
                    },
                )
            report = still_conflict  # round 1 conflict -> retry with fresh report

        raise ReviewError(  # pragma: no cover — loop always exits via return/raise
            "regeneration failed to resolve conflict after 2 rounds",
            details={"original_fact": fact.statement},
        )

    def _find_scene_for_fact(self, fact: Fact) -> StoryScene | None:
        """Best-effort: locate the scene that produced this fact."""
        # Match by scene_id from the fact's episode_id (if set) or by
        # checking if the fact's subject appears in the scene's text.
        if fact.scene_id:
            for scene in self.story.scenes:
                if scene.scene_id == fact.scene_id:
                    return scene
        for scene in self.story.scenes:
            if fact.subject.lower() in scene.narration_text.lower():
                return scene
        return None

    def _regenerate_scene_narration(
        self, ctx: StageContext, scene: StoryScene, report: ConflictReport
    ) -> str:
        """Call the writer LLM to regenerate a scene's narration, with the
        conflict report injected as feedback."""
        from storyforge.providers.llm import LLMClient, fill_prompt, load_prompt

        client = LLMClient(ctx.settings, ctx.settings.llm.writer_model)
        template = load_prompt("scene")
        appearance_by_name = {c.name: c.appearance for c in self.story.config.characters}
        characters = (
            "; ".join(
                f"{name} ({appearance_by_name.get(name, 'appearance unspecified')})"
                for name in scene.beat.characters
            )
            or "(narrator only)"
        )
        conflict_feedback = (
            f"\n\nCONFLICT REPORT (resolve this before writing):\n"
            f"  - Fact: {report.reason}"
        )
        system = fill_prompt(
            template,
            {
                "language": self.story.config.language,
                "tone": self.story.config.style.tone,
                "art_style": self.story.config.style.art_style,
            },
        )
        user = (
            fill_prompt(
                template,
                {
                    "beat_summary": scene.beat.summary,
                    "characters": characters,
                    "degraded": "",
                    "palette": "",
                    "established": "",
                    "style_stats": "",
                },
            )
            + conflict_feedback
        )
        response = client.chat(system, user)
        # Parse only the narration part (before IMAGE_PROMPT:).
        narration, _, _ = response.partition("IMAGE_PROMPT:")
        return narration.strip() or response.strip()

    def _extract_scene_facts(self, ctx: StageContext, scene: StoryScene) -> list[Fact]:
        """Extract facts from a single scene (reuses the review_extract prompt)."""
        from storyforge.providers.llm import LLMClient, fill_prompt, load_prompt

        client = LLMClient(ctx.settings, ctx.settings.llm.writer_model)
        template = load_prompt("review_extract")
        scenes_text = (
            f"SCENE {scene.scene_id} (beat {scene.beat.beat_id}"
            + ("\n" + scene.narration_text)
            + (f"\nfacts_used: {', '.join(scene.facts_used)}" if scene.facts_used else "")
        )
        response = client.chat(
            system=fill_prompt(template, {"language": self.story.config.language}),
            user=fill_prompt(template, {"scenes": scenes_text}),
        )
        return parse_fact_lines(response)

    def _build_ledger(self, ctx: StageContext) -> LedgerStore:
        from storyforge.ledger import build_ledger
        from storyforge.ledger.arbiter import build_arbiter_ledger

        universe = self.story.config.universe
        inner = build_ledger(Path(ctx.settings.knowledge.ledgers_dir) / universe)
        # M4-B4: wrap with the LLM arbiter when enabled (off by default).
        return build_arbiter_ledger(ctx.settings, inner.universe_dir, inner)  # type: ignore[attr-defined]

    def _scene_declares_twist(self, subject: str) -> bool:
        lowered = subject.strip().lower()
        return any(
            scene.beat.intent == "twist"
            and (
                lowered in [c.lower() for c in scene.beat.characters]
                or lowered in scene.narration_text.lower()
            )
            for scene in self.story.scenes
        )
