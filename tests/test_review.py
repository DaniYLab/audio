"""M3-V2 reviewer tests — the two P3 trap tests (m3_design.md §2.2).

1. Trap (recall): a draft fact contradicting the ledger MUST be CONFLICT.
2. Twist false-positive (precision): the SAME contradiction inside a beat
   declared intent="twist" MUST be TWIST_OK, never CONFLICT.

Both must pass before the reviewer is accepted — run them with a real
ledger store and a deterministic (fake) extractor.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.exceptions import StoryForgeError
from storyforge.core.types import (
    GroundingLevel,
    RunManifest,
    Story,
    StoryBeat,
    StoryConfig,
    StoryScene,
)
from storyforge.kb.types import Fact, FactKind, FactOrigin
from storyforge.ledger import build_ledger
from storyforge.stages.review import ReviewArtifact, ReviewStage, parse_fact_lines


def _fact(statement: str, origin: FactOrigin = FactOrigin.INVENTED) -> Fact:
    return Fact(
        fact_id="f_ext0000",
        kind=FactKind.CHARACTER,
        subject="Bà Ngoại",
        statement=statement,
        origin=origin,
        chunk_refs=["ep07:0012"] if origin is FactOrigin.CITED else [],
        episode_id="pending",
        extracted_by="llm",
    )


def _story(*, twist: bool = False, grounding: GroundingLevel = GroundingLevel.LOOSE) -> Story:
    beat = StoryBeat(
        beat_id="beat_02",
        summary="Bà Ngoại qua đời",
        characters=["Bà Ngoại"],
        intent="twist" if twist else "normal",
    )
    scene = StoryScene(
        scene_id="beat_02_scene",
        beat=beat,
        narration_text="Bà Ngoại đã qua đời mùa đông năm ấy, trước hiên nhà.",
        image_prompt="an old woman at a winter doorway",
    )
    config = StoryConfig(
        title="Test",
        genre="test",
        universe="storyvu",
        grounding=grounding,
    )
    return Story(config=config, outline=[beat], scenes=[scene])


@pytest.fixture()
def ctx(tmp_path: Path) -> StageContext:
    settings = Settings(
        llm__api_key="test-key",
        workspace_dir=tmp_path / "ws",
        knowledge__ledgers_dir=tmp_path / "ledgers",
    )
    store = ArtifactStore(settings.workspace_dir, "proj")
    return StageContext(settings, store, RunManifest(project="proj"))


def _ledger_with_alive(ctx: StageContext, suffix: str = ""):
    # UUID so every call is isolated — tests never collide on disk.
    uid = uuid4().hex[:8]
    ledger = build_ledger(Path(ctx.settings.knowledge.ledgers_dir) / f"storyvu{suffix}_{uid}")
    ledger.record_episode(
        "ep_003",
        [
            Fact(
                fact_id="f0007",
                kind=FactKind.CHARACTER,
                subject="Bà Ngoại",
                statement="Bà Ngoại còn sống, ở cùng Lan",
                origin=FactOrigin.INVENTED,
                episode_id="ep_003",
            )
        ],
    )
    return ledger


def test_trap_recall_loose_flag_conflict(ctx: StageContext):
    """Trap 1: draft says 'qua đời' vs ledger 'còn sống' -> CONFLICT (loose)."""
    ledger = _ledger_with_alive(ctx, "_trap1")
    stage = ReviewStage(
        _story(), extractor=lambda story: [_fact("Bà Ngoại đã qua đời")], ledger=ledger
    )
    artifact: ReviewArtifact = stage.run(ctx, force=True)
    assert artifact.summary.n_conflict == 1
    assert artifact.needs_review == ["f_ext0000"]
    report = artifact.reports[0]
    assert report.verdict.value == "conflict"
    assert [f.fact_id for f in report.conflicts] == ["f0007"]


def test_trap_recall_strict_fails_fast(ctx: StageContext):
    """Trap 1 strict: the contradiction must fail the stage."""
    ledger = _ledger_with_alive(ctx, "_trap1strict")
    stage = ReviewStage(
        _story(grounding=GroundingLevel.STRICT),
        extractor=lambda story: [_fact("Bà Ngoại đã qua đời")],
        ledger=ledger,
    )
    with pytest.raises(StoryForgeError, match="conflicts with established canon"):
        stage.run(ctx, force=True)


def test_twist_false_positive_is_twist_ok(ctx: StageContext):
    """Trap 2: same contradiction, beat declares intent=twist -> TWIST_OK."""
    ledger = _ledger_with_alive(ctx, "_twist")
    stage = ReviewStage(
        _story(twist=True),
        extractor=lambda story: [_fact("Bà Ngoại đã qua đời")],
        ledger=ledger,
    )
    artifact: ReviewArtifact = stage.run(ctx, force=True)
    assert artifact.reports[0].verdict.value == "twist_ok"
    assert artifact.summary.n_conflict == 0
    assert artifact.summary.n_twist == 1
    # Twist facts still go to facts_to_record (supersede applied at ship time).
    assert artifact.facts_to_record


def test_review_artifact_written_and_resumable(ctx: StageContext):
    ledger = _ledger_with_alive(ctx, "_artifact")
    stage = ReviewStage(_story(), extractor=lambda story: [], ledger=ledger)
    artifact = stage.run(ctx, force=True)
    artifact = stage.run(ctx, force=True)
    out = ctx.store.dir("04_story") / "review.json"
    assert out.exists()
    reloaded = ReviewArtifact.model_validate_json(out.read_text(encoding="utf-8"))
    assert reloaded.summary.n_new_facts == 0
    assert artifact.universe == "storyvu"


def test_parse_fact_lines_full_and_edge_cases():
    response = (
        "FACT: character | Bà Ngoại | Bà Ngoại gánh hàng rong | invented | -\n"
        "FACT: event | Lan | Lan mất chiếc lá | cited | ep07:0012, ep07:0015\n"
        "FACT: setting | Chợ Đồng Xuân | Chợ họp từ tờ mờ đất | inferred | -\n"
        "bogus line without prefix\n"
        "FACT: badkind | X | Y | invented | -"
    )
    facts = parse_fact_lines(response)
    assert len(facts) == 3
    assert facts[0].subject == "Bà Ngoại" and facts[0].origin is FactOrigin.INVENTED
    assert facts[1].origin is FactOrigin.CITED
    assert facts[1].chunk_refs == ["ep07:0012", "ep07:0015"]
    assert facts[2].origin is FactOrigin.INFERRED
    # CITED without refs downgrades to inferred (invalid contract).
    downgraded = parse_fact_lines("FACT: character | X | Y còn sống | cited | -")
    assert downgraded and downgraded[0].origin is FactOrigin.INFERRED
