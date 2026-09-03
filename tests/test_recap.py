"""M4-A2 recap tests — deterministic plan builder (no ffmpeg needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.core.types import StoryConfig
from storyforge.kb.episode_summary import EpisodeSummary, EpisodeSummaryStore, SummaryMoment
from storyforge.kb.types import Fact, FactKind, FactOrigin
from storyforge.ledger import build_ledger, load_universe
from storyforge.recap import build_recap_plan, should_recap


def _fact(fact_id: str, statement: str, episode: str) -> Fact:
    return Fact(
        fact_id=fact_id,
        kind=FactKind.EVENT,
        subject="Lan",
        statement=statement,
        origin=FactOrigin.INVENTED,
        episode_id=episode,
    )


@pytest.fixture()
def universe(tmp_path: Path) -> Path:
    root = tmp_path / "ledgers" / "storyvu"
    ledger = build_ledger(root)
    ledger.record_episode("ep_001", [_fact("f0001", "Lan mất chiếc lá đỏ trong mưa", "ep_001")])
    ledger.record_episode("ep_002", [_fact("f0002", "Bà Ngoại tặng Lan chiếc lá mới", "ep_002")])
    return root


@pytest.fixture()
def config() -> StoryConfig:
    return StoryConfig(title="Test", genre="test", universe="storyvu")


def test_should_recap_episode1_is_off(config: StoryConfig):
    enabled, reason = should_recap(config, 1)
    assert enabled is False and "episode 1" in (reason or "")


def test_should_recap_episode2_on(config: StoryConfig):
    enabled, _ = should_recap(config, 2)
    assert enabled is True


def test_should_recap_recap_off(config: StoryConfig):
    config.recap = False
    enabled, reason = should_recap(config, 2)
    assert enabled is False and "recap=off" in (reason or "")


def test_build_recap_plan_episode1_empty(universe: Path, config: StoryConfig):
    plan = build_recap_plan(
        config,
        1,
        load_universe(universe),
        EpisodeSummaryStore(universe.parent / "kb"),
        universe.parent,
    )
    assert plan.enabled is False


def test_build_recap_plan_episode2_builds_script(universe: Path, config: StoryConfig):
    ledger = load_universe(universe)
    summaries = EpisodeSummaryStore(universe.parent / "kb")
    plan = build_recap_plan(config, 2, ledger, summaries, universe.parent)

    assert plan.enabled is True
    assert 0 < plan.word_count <= 90  # AC2: 20-30s (~60-90 words cap)
    assert "chiếc lá" in plan.script


def test_build_recap_plan_uses_summary_color(universe: Path, config: StoryConfig):
    from storyforge.ledger import load_universe

    ledger = load_universe(universe)
    summaries = EpisodeSummaryStore(universe.parent / "kb")
    summaries.save(
        EpisodeSummary(
            source_id="ep002",
            universe_id="storyvu",
            moments=[
                SummaryMoment(person="Lan", place="bờ sông", action="cầm chiếc lá đỏ dưới mưa")
            ],
        )
    )
    plan = build_recap_plan(config, 2, ledger, summaries, universe.parent)
    assert plan.enabled and "cầm chiếc lá đỏ dưới mưa" in plan.script


def test_build_recap_plan_no_ledger_yet(tmp_path: Path, config: StoryConfig):
    from storyforge.ledger import load_universe

    empty = tmp_path / "ledgers" / "fresh"
    plan = build_recap_plan(config, 2, load_universe(empty), EpisodeSummaryStore(empty), empty)
    assert plan.enabled is False and "no established facts" in (plan.skip_reason or "")


def test_recap_plan_word_cap_trims(universe: Path, config: StoryConfig):
    ledger = build_ledger(universe)
    for i in range(12):
        ledger.record_episode(
            f"ep_{100+i:03d}",
            [
                _fact(
                    f"f{i+100:04d}",
                    "Một câu chuyện dài về ký ức tuổi thơ ở nông thôn miền Bắc ngày xưa.",
                    f"ep_{100+i:03d}",
                )
            ],
        )
    plan = build_recap_plan(
        config,
        2,
        load_universe(universe),
        EpisodeSummaryStore(universe.parent / "kb"),
        universe.parent,
    )
    assert plan.word_count <= 90


def test_recap_plan_as_of_excludes_future_facts(tmp_path: Path, config: StoryConfig):
    """M6-V2: as_of_episode keeps the recap anchored at the previous episode —
    a superseding fact from the future must not leak into it."""
    root = tmp_path / "ledgers" / "storyvu_asof"
    store = build_ledger(root)
    store.record_episode("ep_001", [_fact("f0001", "Lan mất chiếc lá đỏ trong mưa", "ep_001")])
    store.record_episode("ep_002", [_fact("f0002", "Bà Ngoại tặng Lan chiếc lá mới", "ep_002")])
    # ep_003 supersedes f0001 — but a recap of ep_002 (as_of ep_002) must not
    # know about it.
    store.record_episode(
        "ep_003",
        [_fact("f0003", "Lan tìm thấy chiếc lá trong hộp gỗ cũ", "ep_003")],
    )

    ledger = load_universe(root)
    plan = build_recap_plan(
        config,
        3,
        ledger,
        EpisodeSummaryStore(root.parent / "kb"),
        root.parent,
        as_of_episode="ep_002",
    )
    assert plan.enabled
    # Only facts valid at ep_002 appear — the ep_003 fact is invisible.
    assert "chiếc lá trong hộp gỗ" not in plan.script


def test_recap_plan_as_of_after_supersede_uses_replacement(tmp_path: Path, config: StoryConfig):
    root = tmp_path / "ledgers" / "storyvu_sup"
    store = build_ledger(root)
    store.record_episode("ep_001", [_fact("f0001", "Lan mất chiếc lá đỏ trong mưa", "ep_001")])
    store.supersede(
        "f0001",
        _fact("f0005", "Lan tìm lại được chiếc lá đỏ", "ep_003"),
        actor="reviewer",
    )

    ledger = load_universe(root)
    plan = build_recap_plan(
        config,
        4,
        ledger,
        EpisodeSummaryStore(root.parent / "kb"),
        root.parent,
        as_of_episode="ep_003",
    )
    assert plan.enabled
    # The replacement fact is visible at ep_003.
    assert "tìm lại được" in plan.script
