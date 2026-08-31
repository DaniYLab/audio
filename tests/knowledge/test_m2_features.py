"""M2-V1/V2/V5 feature tests — reranker flag + EpisodeSummary persistence.

Conformance-style: memory backend always, Qdrant when reachable (skipped in
CI by default per CONVENTIONS.md §9). Reranker and summarizer are injected
fakes so no model or network is touched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from storyforge.kb.episode_summary import EpisodeSummary, SummaryMoment
from storyforge.kb.types import KnowledgeStore, SearchQuery
from tests.knowledge.test_conformance import (
    _UNIVERSE,
    EPISODES,
    FakeEmbedder,
    _qdrant_available,
    _settings,
    _transcript,
)

_UNIVERSE_M2 = f"{_UNIVERSE}_m2"

StoreFactory = Callable[[], KnowledgeStore]


class CoverageReranker:
    """Deterministic fake: boosts hits covering more query terms first."""

    def rerank(self, query: str, hits: list[object]) -> list[object]:
        terms = set(query.lower().split())

        def coverage(hit: object) -> int:
            text = str(getattr(hit, "text", "")).lower()
            return len(terms & set(text.split()))

        return sorted(hits, key=lambda hit: (-coverage(hit), hits.index(hit)))  # type: ignore[arg-type]


class FakeSummarizer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def summarize(self, transcript: object) -> EpisodeSummary:
        source_id = str(transcript.source.id)  # type: ignore[attr-defined]
        self.calls.append(source_id)
        return EpisodeSummary(
            source_id=source_id,
            universe_id=_UNIVERSE_M2,
            moments=[
                SummaryMoment(
                    person="bà Ngoại",
                    place="chợ Đồng Xuân",
                    action="gánh hàng rong",
                    detail="sương sớm",
                )
            ],
            model="fake",
        )


class BoomSummarizer:
    def summarize(self, transcript: object) -> EpisodeSummary:
        raise RuntimeError("llm exploded")


def _memory_factory_m2(tmp_path: Path, reranker: object, summarizer: object) -> StoreFactory:
    settings = _settings(tmp_path, "memory")

    def make() -> KnowledgeStore:
        from storyforge.kb.memory_store import InMemoryKnowledgeStore

        return InMemoryKnowledgeStore(
            settings, _UNIVERSE_M2, reranker=reranker, summarizer=summarizer
        )

    return make


def _qdrant_factory_m2(tmp_path: Path, reranker: object, summarizer: object) -> StoreFactory | None:
    settings = _settings(tmp_path, "qdrant")
    if not _qdrant_available(settings.knowledge.qdrant_url):
        return None
    universe = f"{_UNIVERSE_M2}_{uuid4().hex[:8]}"

    def make() -> KnowledgeStore:
        from storyforge.kb.qdrant_store import QdrantKnowledgeStore

        return QdrantKnowledgeStore(
            settings,
            universe,
            embedder=FakeEmbedder(),
            reranker=reranker,
            summarizer=summarizer,
        )

    return make


@pytest.fixture(params=["memory", "qdrant"])
def m2_store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[str, KnowledgeStore, FakeSummarizer]:
    summarizer = FakeSummarizer()
    factories: dict[str, StoreFactory] = {
        "memory": _memory_factory_m2(tmp_path, CoverageReranker(), summarizer)
    }
    qdrant = _qdrant_factory_m2(tmp_path, CoverageReranker(), summarizer)
    if qdrant is not None:
        factories["qdrant"] = qdrant
    if request.param not in factories:
        pytest.skip("qdrant backend not available")
    return request.param, factories[request.param](), summarizer


# --- M2-V1: reranker flag ---------------------------------------------------------


def _term_coverage(text: str, query: str) -> int:
    return len(set(query.lower().split()) & set(text.lower().split()))


def test_reranker_boosts_full_coverage_hits(
    m2_store: tuple[str, KnowledgeStore, FakeSummarizer],
):
    backend, store, _ = m2_store
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))

    query = "bà Ngoại gánh hàng rong chợ"
    plain = store.search(SearchQuery(text=query, top_k=5))
    boosted = store.search(SearchQuery(text=query, top_k=5, use_reranker=True))

    assert plain, "sanity: retrieval must return hits"
    assert boosted, "reranked search must also return hits"
    if backend == "memory":
        # The fake reranker sorts by query-term coverage, so the reranked
        # top hit must cover at least as many query terms as the plain one.
        assert _term_coverage(str(boosted[0].text), query) >= _term_coverage(
            str(plain[0].text), query
        )
    # Both backends: reranking never leaks other universes' data.
    assert {h.source_id for h in boosted} <= set(EPISODES)


def test_reranker_flag_default_off_is_deterministic(
    m2_store: tuple[str, KnowledgeStore, FakeSummarizer],
):
    """Without use_reranker the injected reranker must not run — two plain
    searches return identical orderings."""
    _, store, _ = m2_store
    store.ingest(_transcript("ep01", EPISODES["ep01"]))
    first = store.search(SearchQuery(text="bà Ngoại chợ", top_k=3))
    second = store.search(SearchQuery(text="bà Ngoại chợ", top_k=3))
    assert [h.chunk_id for h in first] == [h.chunk_id for h in second]


# --- M2-V2: EpisodeSummary ----------------------------------------------------------


def test_episode_summary_saved_on_fresh_ingest(
    m2_store: tuple[str, KnowledgeStore, FakeSummarizer],
):
    _, store, summarizer = m2_store
    report = store.ingest(_transcript("ep01", EPISODES["ep01"]))
    assert report.status == "ingested"
    assert summarizer.calls == ["ep01"]

    summaries = store._summaries
    universe = str(store._universe_id)
    loaded = summaries.load(universe, "ep01")
    assert loaded is not None
    assert loaded.source_id == "ep01"
    assert loaded.moments and loaded.moments[0].person == "bà Ngoại"


def test_episode_summary_not_called_on_noop(
    m2_store: tuple[str, KnowledgeStore, FakeSummarizer],
):
    _, store, summarizer = m2_store
    store.ingest(_transcript("ep01", EPISODES["ep01"]))
    store.ingest(_transcript("ep01", EPISODES["ep01"]))
    assert summarizer.calls == ["ep01"]  # second ingest is a no-op


def test_summary_failure_never_fails_ingest(tmp_path: Path):
    """A crashing summarizer must not turn a good ingest into a KB failure."""
    from storyforge.kb.memory_store import InMemoryKnowledgeStore

    settings = _settings(tmp_path, "memory")
    store = InMemoryKnowledgeStore(settings, _UNIVERSE_M2, summarizer=BoomSummarizer())
    report = store.ingest(_transcript("ep01", EPISODES["ep01"]))
    assert report.status == "ingested"
    assert report.chunks_written > 0
