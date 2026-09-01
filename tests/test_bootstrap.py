"""M4-B5 bootstrap tests — candidate collection + draft generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.bootstrap import BootstrapDraft, build_bootstrap_draft, collect_candidates
from storyforge.kb.alias import AliasStore
from storyforge.kb.types import EntityFactLine, EntityFacts


class FakeBootstrapStore:
    """Minimal KnowledgeStore that returns canned dossiers (no network)."""

    def __init__(self, dossiers: dict[str, EntityFacts]) -> None:
        self._dossiers = dossiers

    def query_entities(self, name: str) -> EntityFacts | None:
        return self._dossiers.get(name.lower())

    def health(self) -> bool:
        return True

    def search(self, query: object, **kwargs: object) -> list[object]:
        return []

    def similar_sources(self, source_id: str) -> list[object]:
        return []

    def ingest(self, transcript: object) -> object:
        raise NotImplementedError


@pytest.fixture()
def alias(tmp_path: Path) -> AliasStore:
    store = AliasStore(tmp_path / "kb" / "storyvu" / "aliases.yaml")
    store.add_pending("Bà Ngoại", "person")
    store.add_pending("Lan", "person")
    store.add_pending("Chợ Đồng Xuân", "place")
    store.add_pending("Hà Giang", "place")
    return store


@pytest.fixture()
def store_with_dossiers(alias: AliasStore) -> FakeBootstrapStore:
    dossiers: dict[str, EntityFacts] = {}
    for name, type_ in [("Bà Ngoại", "person"), ("Lan", "person"), ("Chợ Đồng Xuân", "place")]:
        dossiers[name.lower()] = EntityFacts(
            canonical=name,
            type=type_,
            mention_count=10,
            sources=["ep01", "ep02"],
            facts=[
                EntityFactLine(
                    statement=f"{name} là một {type_} trong truyện", chunk_id="x", source_id="ep01"
                )
            ],
        )
    return FakeBootstrapStore(dossiers)


def test_collect_candidates_filters_by_type_and_mentions(
    alias: AliasStore, store_with_dossiers: FakeBootstrapStore
):
    dossiers, skipped = collect_candidates(store_with_dossiers, alias)
    assert len(dossiers) == 3  # all 3 meet threshold
    assert all(d.type in ("person", "place") for d in dossiers)
    assert "Hà Giang" not in {d.canonical for d in dossiers}  # no dossier in store


def test_build_bootstrap_draft_with_injected_synthesizer(
    alias: AliasStore, store_with_dossiers: FakeBootstrapStore
):
    draft = build_bootstrap_draft(
        store_with_dossiers,
        alias,
        "storyvu",
        synthesizer=lambda dossiers: BootstrapDraft(
            universe_id="storyvu",
            premise="A story about a grandmother and her granddaughter.",
            world_rules=["The market is the center of life."],
            source_query="chợ quê tuổi thơ",
        ),
    )
    assert draft.universe_id == "storyvu"
    assert draft.premise
    assert draft.characters  # 2 persons: Bà Ngoại, Lan
    assert draft.skipped_entities  # Hà Giang skipped
    assert draft.source_query == "chợ quê tuổi thơ"


def test_collect_candidates_skips_below_threshold(
    alias: AliasStore, store_with_dossiers: FakeBootstrapStore
):
    low = store_with_dossiers._dossiers["bà ngoại"]
    low.mention_count = 0
    dossiers, skipped = collect_candidates(store_with_dossiers, alias)
    assert "Bà Ngoại" not in {d.canonical for d in dossiers}


def test_write_draft_creates_yaml(tmp_path: Path):
    draft = BootstrapDraft(universe_id="storyvu", premise="Test premise.")
    out = tmp_path / "kb" / "storyvu" / "bootstrap_draft.yaml"
    out.parent.mkdir(parents=True)
    import yaml

    out.write_text(yaml.safe_dump(draft.model_dump(), allow_unicode=True))
    assert out.exists()
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["premise"] == "Test premise."
