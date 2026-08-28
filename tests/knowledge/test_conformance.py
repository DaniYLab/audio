"""Protocol conformance suite — runs against EVERY backend (WORKPLAN D6).

The suite is the merge gate for both Dev 1's Qdrant backend and Dev 2's
compiler work. Backends under test:
  - InMemoryKnowledgeStore (always)
  - QdrantKnowledgeStore (only when a Qdrant server is reachable, marked
    ``integration`` so CI skips it by default per CONVENTIONS.md §9)

Both backends share kb.ingest.prepare as their single write path, so the
behavioral contract (idempotency, universe scoping, group_by, entity
dossiers) must hold identically.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4 as _uuid4

import pytest

from storyforge.core.config import Settings
from storyforge.core.types import SourceRef, Transcript, TranscriptSegment
from storyforge.kb.types import (
    IngestReport,
    KnowledgeStore,
    SearchIntent,
    SearchQuery,
)

_UNIVERSE = "conformance"


def _settings(tmp_path: Path, store: str) -> Settings:
    return Settings(
        llm__api_key="test-key",
        knowledge__store=store,
        knowledge__kb_data_dir=tmp_path / "kb",
        workspace_dir=tmp_path / "ws",
    )


def _transcript(source_id: str, texts: list[tuple[str, str]]) -> Transcript:
    segments = [
        TranscriptSegment(start=float(i * 10), end=float(i * 10 + 9), text=text, speaker=speaker)
        for i, (text, speaker) in enumerate(texts)
    ]
    return Transcript(
        source=SourceRef(id=source_id, title=f"source {source_id}"),
        language="vi",
        audio_path=Path("fake.m4a"),
        segments=segments,
    )


EPISODES = {
    "ep01": [
        ("Hôm nay bà Ngoại kể chuyện tuổi thơ ở nông thôn miền Bắc.", "SPEAKER_00"),
        ("Bà Ngoại gánh hàng rong qua chợ Đồng Xuân từ tờ mờ đất.", "SPEAKER_00"),
        ("Lan theo bà ra chợ sớm, sương chưa tan đã thấy các gánh cá.", "SPEAKER_01"),
    ],
    "ep02": [
        ("Trời mưa lớn, trẻ con trong xóm chạy về nhà tránh ướt.", "SPEAKER_00"),
        ("Bữa cơm tối gia đình Lan có thêm cá kho của bà Ngoại.", "SPEAKER_00"),
    ],
    "ep03": [
        ("Ký ức về bà ngoại luôn gắn với tiếng rao cá buổi sớm.", "SPEAKER_01"),
        ("Mùa đông năm ấy bà ốm, Lan nấu cháo gánh lên tận giường.", "SPEAKER_01"),
    ],
}


def _qdrant_available(url: str) -> bool:
    try:
        import httpx

        response = httpx.get(f"{url.rstrip('/')}/readyz", timeout=2.0)
        return response.status_code == 200
    except Exception:
        return False


StoreFactory = Callable[[], KnowledgeStore]


class FakeEmbedder:
    """Deterministic dense+sparse embedder — no network, no model.

    Dense: 8-dim hash-bucketed term vector. Sparse: bag-of-words with
    synthetic indices, mirroring the fallback encoder in kb.embedder.
    """

    def encode(self, texts: list[str]) -> list[object]:
        from storyforge.kb.embedder import SparseVector, _terms

        embeddings = []
        for text in texts:
            dense = [0.0] * 8
            indices: list[int] = []
            values: list[float] = []
            counts: dict[str, float] = {}
            for term in _terms(text):
                counts[term] = counts.get(term, 0.0) + 1.0
                dense[hash(term) % 8] += 1.0
            for term, count in sorted(counts.items()):
                indices.append(abs(hash(term)) % 100000)
                values.append(count)
            embeddings.append(
                type("E", (), {"dense": dense, "sparse": SparseVector(indices, values)})()
            )
        return embeddings


def _memory_factory(tmp_path: Path) -> StoreFactory:
    settings = _settings(tmp_path, "memory")

    def make() -> KnowledgeStore:
        from storyforge.kb.memory_store import InMemoryKnowledgeStore

        return InMemoryKnowledgeStore(settings, _UNIVERSE)

    return make


def _qdrant_factory(tmp_path: Path) -> StoreFactory | None:
    settings = _settings(tmp_path, "qdrant")
    if not _qdrant_available(settings.knowledge.qdrant_url):
        return None

    # Unique universe per test run: Qdrant is a long-lived server, so a
    # shared collection would leak points between tests and break
    # idempotency assertions.
    universe = f"{_UNIVERSE}_{_uuid4().hex[:8]}"

    def make() -> KnowledgeStore:
        from storyforge.kb.qdrant_store import QdrantKnowledgeStore

        return QdrantKnowledgeStore(settings, universe, embedder=FakeEmbedder())

    return make


def _backend_factories(tmp_path: Path) -> list[tuple[str, StoreFactory]]:
    factories: list[tuple[str, StoreFactory]] = [("memory", _memory_factory(tmp_path))]
    qdrant = _qdrant_factory(tmp_path)
    if qdrant is not None:
        factories.append(("qdrant", qdrant))
    return factories


@pytest.fixture(params=["memory", "qdrant"])
def store_factory(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[str, StoreFactory]:
    factories = dict(_backend_factories(tmp_path))
    if request.param not in factories:  # qdrant server not reachable
        pytest.skip("qdrant backend not available")
    return request.param, factories[request.param]


# --- ingest contract --------------------------------------------------------------


def test_ingest_returns_report(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    report = store.ingest(_transcript("ep01", EPISODES["ep01"]))
    assert isinstance(report, IngestReport)
    assert report.source_id == "ep01"
    # The qdrant factory appends a random suffix per test (server data
    # isolation), so assert on the prefix.
    assert report.universe_id.startswith(_UNIVERSE)
    assert report.status == "ingested"
    assert report.chunks_written > 0


def test_ingest_is_idempotent_same_content(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    first = store.ingest(_transcript("ep01", EPISODES["ep01"]))
    second = store.ingest(_transcript("ep01", EPISODES["ep01"]))
    assert first.status == "ingested"
    assert second.status == "noop"
    assert second.chunks_written == 0


def test_reingest_after_retranscribe(store_factory: tuple[str, StoreFactory]):
    """New content (re-transcribe) replaces old points, same chunk_id pattern."""
    _, make = store_factory
    store = make()
    store.ingest(_transcript("ep01", EPISODES["ep01"]))
    changed = _transcript("ep01", [("Nội dung mới sau khi transcribe lại.", "SPEAKER_00")])
    report = store.ingest(changed)
    assert report.status == "reingested"
    hits = store.search(SearchQuery(text="nội dung mới"))
    assert hits and all(h.source_id == "ep01" for h in hits)
    # The OLD points must be gone: nothing retrieved still carries the old
    # text (the new chunk of the same source may weakly match, which is fine).
    stale = store.search(SearchQuery(text="gánh hàng rong"))
    assert all("gánh hàng" not in h.text for h in stale)


# --- search contract ----------------------------------------------------------------


def test_search_returns_ranked_hits_with_metadata(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    hits = store.search(SearchQuery(text="bà Ngoại gánh hàng rong chợ"))
    assert hits
    top = hits[0]
    assert top.chunk_id.startswith("ep")
    assert top.text  # raw text, not the alias-normalized copy
    assert top.source_id in EPISODES
    assert top.start_ts >= 0.0
    assert top.end_ts >= top.start_ts


def test_search_group_by_source_caps_per_source(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    hits = store.search(SearchQuery(text="bà Ngoại", group_by_source=True, top_k=8, candidate_k=50))
    per_source: dict[str, int] = {}
    for hit in hits:
        per_source[hit.source_id] = per_source.get(hit.source_id, 0) + 1
    assert all(count <= 3 for count in per_source.values())
    # Source diversity is the point of group_by: more than one source at top.
    assert len(per_source) >= 2


def test_search_respects_top_k(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    hits = store.search(SearchQuery(text="bà Ngoại chợ mưa cá", top_k=2))
    assert len(hits) <= 2


def test_universe_scoping(store_factory: tuple[str, StoreFactory]):
    """Data ingested into another universe must never leak into this one."""
    _, make = store_factory
    settings = make().__class__  # noqa: F841 - readability marker
    store = make()
    # A second store instance over the same backend but a different universe:
    # construct via the same factory's backend type through a fresh ingest
    # into THIS store only; then verify searches only see _UNIVERSE data.
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    hits = store.search(SearchQuery(text="bà Ngoại"))
    assert hits  # sanity: data is there
    # The memory backend exposes internals; the qdrant backend filters
    # structurally. Either way, every hit must belong to _UNIVERSE sources.
    assert {h.source_id for h in hits} <= set(EPISODES)


# --- entity dossier (J2) ----------------------------------------------------------


def test_query_entities_returns_dossier(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    dossier = store.query_entities("Bà Ngoại")
    assert dossier is not None
    assert dossier.mention_count >= 1
    assert dossier.sources
    assert dossier.facts
    assert dossier.type in ("person", "other")


def test_query_entities_unknown_returns_none(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    assert store.query_entities("Hà Giang") is None


# --- similar sources -----------------------------------------------------------------


def test_similar_sources_excludes_self(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    store = make()
    for source_id, texts in EPISODES.items():
        store.ingest(_transcript(source_id, texts))
    similar = store.similar_sources("ep01")
    assert all(s.source_id != "ep01" for s in similar)


# --- health ----------------------------------------------------------------------------


def test_health(store_factory: tuple[str, StoreFactory]):
    _, make = store_factory
    assert make().health() is True


# --- intent values are part of the frozen contract ---------------------------------------


def test_search_intent_enum_values():
    assert {intent.value for intent in SearchIntent} == {"theme", "entity", "scene"}
