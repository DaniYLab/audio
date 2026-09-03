"""M6-V2 bi-temporal tests — query(as_of_episode) returns facts valid at a point."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.kb.types import Fact, FactKind, FactOrigin
from storyforge.ledger import build_ledger


def _fact(fact_id: str, statement: str, episode: str) -> Fact:
    return Fact(
        fact_id=fact_id,
        kind=FactKind.CHARACTER,
        subject="Bà Ngoại",
        statement=statement,
        origin=FactOrigin.INVENTED,
        episode_id=episode,
    )


@pytest.fixture()
def ledger(tmp_path: Path):
    store = build_ledger(tmp_path / "ledgers" / "storyvu")
    store.record_episode("ep_001", [_fact("f0001", "Bà Ngoại còn sống", "ep_001")])
    store.record_episode("ep_002", [_fact("f0002", "Lan tìm thấy chiếc lá đỏ", "ep_002")])
    # f0001 is superseded at ep_015 by f0041.
    store.supersede(
        "f0001",
        _fact("f0041", "Bà Ngoại đã qua đời", "ep_015"),
        actor="reviewer",
    )
    return store


def test_as_of_before_supersede_returns_old_fact(ledger):
    # At ep_010 the ledger still knows "còn sống" (superseded at ep_015).
    facts = ledger.query(as_of_episode="ep_010")
    statements = {f.fact_id: f.statement for f in facts}
    assert statements["f0001"] == "Bà Ngoại còn sống"
    assert "f0041" not in statements  # not established yet


def test_as_of_after_supersede_returns_new_fact(ledger):
    facts = ledger.query(as_of_episode="ep_020")
    statements = {f.fact_id: f.statement for f in facts}
    assert "f0001" not in statements  # superseded
    assert statements["f0041"] == "Bà Ngoại đã qua đời"


def test_as_of_current_matches_default(ledger):
    assert {f.fact_id for f in ledger.query()} == {f.fact_id for f in ledger.query(as_of_episode="ep_999")}


def test_as_of_subject_filter(ledger):
    # f0001 + f0002 both have subject "Bà Ngoại" in the fixture; at ep_010 the
    # superseding f0041 does not exist yet.
    facts = ledger.query(subject="Bà Ngoại", as_of_episode="ep_010")
    assert {f.fact_id for f in facts} == {"f0001", "f0002"}


def test_as_of_unknown_episode_returns_all(ledger):
    # Unknown episode = no time anchor (returns everything live).
    facts = ledger.query(as_of_episode="ep_unknown")
    assert {f.fact_id for f in facts} == {"f0002", "f0041"}


# -- loader.facts_as_of (recap/writer view) ------------------------------------


@pytest.fixture()
def universe_dir(tmp_path: Path) -> Path:
    root = tmp_path / "ledgers" / "storyvu_loader"
    store = build_ledger(root)
    store.record_episode("ep_001", [_fact("f0001", "Bà Ngoại còn sống", "ep_001")])
    store.record_episode("ep_002", [_fact("f0002", "Lan tìm thấy chiếc lá đỏ", "ep_002")])
    store.supersede(
        "f0001",
        _fact("f0041", "Bà Ngoại đã qua đời", "ep_015"),
        actor="reviewer",
    )
    return root


def test_loader_facts_as_of_before_supersede(universe_dir: Path):
    from storyforge.ledger.loader import facts_as_of, load_universe

    universe = load_universe(universe_dir)
    statements = {f.fact_id: f.statement for f in facts_as_of(universe, "ep_010")}
    assert statements["f0001"] == "Bà Ngoại còn sống"
    assert "f0041" not in statements


def test_loader_facts_as_of_after_supersede(universe_dir: Path):
    from storyforge.ledger.loader import facts_as_of, load_universe

    universe = load_universe(universe_dir)
    statements = {f.fact_id: f.statement for f in facts_as_of(universe, "ep_020")}
    assert "f0001" not in statements
    assert statements["f0041"] == "Bà Ngoại đã qua đời"


def test_loader_facts_as_of_release_order(universe_dir: Path):
    """Facts come back in release order (recap takes the tail as 'recent')."""
    from storyforge.ledger.loader import facts_as_of, load_universe

    universe = load_universe(universe_dir)
    ids = [f.fact_id for f in facts_as_of(universe, "ep_002")]
    assert ids == ["f0001", "f0002"]


def test_loader_facts_as_of_unknown_anchor_is_current(universe_dir: Path):
    from storyforge.ledger.loader import facts_as_of, load_universe

    universe = load_universe(universe_dir)
    ids = {f.fact_id for f in facts_as_of(universe, "ep_bogus")}
    assert ids == {"f0002", "f0041"}
