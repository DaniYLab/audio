"""Unit tests for the BriefCompiler — pure logic, mocked store (WORKPLAN W8).

Covers the four W8 behaviors: budget truncate, citation dedup, UNKNOWN
marking, and degrade rendering. No backend, no network.
"""

from __future__ import annotations

import pytest

from storyforge.core.exceptions import KnowledgeBaseError
from storyforge.core.types import (
    CharacterSheet,
    GroundingLevel,
    StoryBeat,
    StoryConfig,
)
from storyforge.kb.brief import render_degraded
from storyforge.kb.compiler import BriefCompiler
from storyforge.kb.types import EntityFacts, SearchHit


def _hit(chunk_id: str, source_id: str = "s1") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        text=f"text of {chunk_id}",
        score=1.0,
        source_id=source_id,
        start_ts=0.0,
        end_ts=1.0,
        speaker="SPEAKER_00",
    )


class _FakeStore:
    def __init__(
        self,
        *,
        healthy: bool = True,
        hits: list[SearchHit] | None = None,
        entities: dict[str, EntityFacts | None] | None = None,
    ) -> None:
        self._healthy = healthy
        self._hits = hits or []
        self._entities = entities or {}

    def ingest(self, transcript: object) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    def search(self, query: object) -> list[SearchHit]:
        if not self._healthy:
            raise KnowledgeBaseError("knowledge store unavailable")
        return list(self._hits)

    def query_entities(self, name: str) -> EntityFacts | None:
        if not self._healthy:
            raise KnowledgeBaseError("knowledge store unavailable")
        return self._entities.get(name)

    def similar_sources(self, source_id: str) -> list[object]:
        return []

    def health(self) -> bool:
        return self._healthy


def _config(grounding: GroundingLevel = GroundingLevel.LOOSE) -> StoryConfig:
    return StoryConfig(
        title="Test Story",
        genre="test",
        universe="demo",
        premise="a premise",
        characters=[CharacterSheet(name="Lan", appearance="girl", personality="curious")],
        grounding=grounding,
    )


def _beat() -> StoryBeat:
    return StoryBeat(beat_id="beat_01", summary="Lan walks", characters=["Lan"], image_hint="")


def test_build_degrades_when_store_unhealthy():
    compiler = BriefCompiler(_FakeStore(healthy=False), _config())
    brief = compiler.build()
    assert brief.degraded is True
    assert brief.reason is not None


def test_build_strict_raises_when_store_unhealthy():
    compiler = BriefCompiler(_FakeStore(healthy=False), _config(grounding=GroundingLevel.STRICT))
    with pytest.raises(KnowledgeBaseError):
        compiler.build()


def test_build_marks_unknown_entities():
    compiler = BriefCompiler(_FakeStore(entities={}), _config())
    brief = compiler.build()
    assert brief.unknown_entities == ["Lan"]


def test_build_populates_dossiers():
    dossier = EntityFacts(
        canonical="Lan", aliases=[], type="person", mention_count=3, sources=["s1"]
    )
    compiler = BriefCompiler(_FakeStore(entities={"Lan": dossier}), _config())
    brief = compiler.build()
    assert len(brief.dossiers) == 1
    assert brief.dossiers[0].canonical == "Lan"


def test_update_truncates_palette_to_budget():
    hits = [_hit(f"c{i}") for i in range(5)]
    compiler = BriefCompiler(_FakeStore(hits=hits), _config())
    brief = compiler.build()
    updated = compiler.update(brief, _beat())
    assert len(updated.palette) == 4


def test_update_dedupes_citations_across_scenes():
    hits = [_hit("c1"), _hit("c2")]
    compiler = BriefCompiler(_FakeStore(hits=hits), _config())
    brief = compiler.build()
    first = compiler.update(brief, _beat())
    assert [p.chunk_id for p in first.palette] == ["c1", "c2"]
    second = compiler.update(brief, _beat())
    assert second.palette == []  # both passages already cited


def test_search_failure_degrades_in_loose():
    class _RaisingStore(_FakeStore):
        def search(self, query: object) -> list[SearchHit]:
            raise KnowledgeBaseError("boom")

    compiler = BriefCompiler(_RaisingStore(), _config())
    brief = compiler.build()
    assert brief.degraded is True
    assert brief.reason is not None


def test_render_degraded_section():
    text = render_degraded("qdrant unavailable")
    assert "[KB DEGRADED:" in text
    assert "qdrant unavailable" in text


def test_story_config_requires_universe():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StoryConfig(title="t", genre="g")
    with pytest.raises(ValidationError):
        StoryConfig(title="t", genre="g", universe="   ")


def test_build_forwards_as_of_episode_to_ledger():
    """M6-V2: BriefCompiler threads ``ledger_as_of`` into the ledger query so
    the writer never sees facts established after the anchor episode."""
    from storyforge.kb.types import Fact, FactKind, FactOrigin

    seen: dict[str, object] = {}

    class _FakeLedger:
        def query(
            self,
            subject: str | None = None,
            kind: object = None,
            include_superseded: bool = False,
            as_of_episode: str | None = None,
        ) -> list[Fact]:
            seen["subject"] = subject
            seen["as_of_episode"] = as_of_episode
            return [
                Fact(
                    fact_id="f0001",
                    kind=FactKind.CHARACTER,
                    subject="Lan",
                    statement="Lan có chiếc lá đỏ",
                    origin=FactOrigin.INVENTED,
                    episode_id="ep_001",
                )
            ]

    config = _config()
    config.characters = [
        CharacterSheet(name="Lan", appearance="girl", personality="curious")
    ]
    compiler = BriefCompiler(
        _FakeStore(), config, ledger=_FakeLedger(), ledger_as_of="ep_002"
    )
    brief = compiler.build()
    assert seen["as_of_episode"] == "ep_002"
    assert brief.invented[0].statement == "Lan có chiếc lá đỏ"


def test_build_as_of_none_queries_current():
    """No anchor → query(as_of_episode=None), i.e. current canon."""
    seen: dict[str, object] = {}

    class _FakeLedger:
        def query(self, subject: str | None = None, **kwargs: object) -> list[object]:
            seen["subject"] = subject
            seen["as_of_episode"] = kwargs.get("as_of_episode")
            return []

    config = _config()
    config.characters = [
        CharacterSheet(name="Lan", appearance="girl", personality="curious")
    ]
    compiler = BriefCompiler(_FakeStore(), config, ledger=_FakeLedger())
    compiler.build()
    assert seen["as_of_episode"] is None
