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
from storyforge.core.types import GroundingLevel, Story
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
            reports.append(report)
            if report.verdict is ConflictVerdict.NO_CONFLICT:
                facts_to_record.append(fact)
            elif report.verdict is ConflictVerdict.TWIST_OK:
                n_twist += 1
                facts_to_record.append(fact)  # supersede applied at record time
            else:  # CONFLICT
                needs_review.append(fact.fact_id)
                if strict:
                    # Auto-regeneration of the scene lands with Dev 2's writer
                    # integration; strict mode fails fast meanwhile (CR note).
                    raise ReviewError(
                        f"fact conflicts with established canon: {report.reason}",
                        details={
                            "fact": fact.statement,
                            "conflicts": [f.fact_id for f in report.conflicts],
                        },
                    )

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

    def _build_ledger(self, ctx: StageContext) -> LedgerStore:
        from storyforge.ledger import build_ledger

        universe = self.story.config.universe
        return build_ledger(Path(ctx.settings.knowledge.ledgers_dir) / universe)

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
