"""Tests for KnowledgeStage — ingest delegation + loose/strict behavior.

The chunking logic itself now lives in ``kb.ingest`` (Dev 1) and is covered by
``tests/knowledge/test_kb_units.py``. This file covers the stage's contract:
delegating to ``store.ingest()``, persisting ``IngestReport``, writing the
``pending_ingest`` marker in loose mode, and failing fast in strict mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.exceptions import KnowledgeBaseError
from storyforge.core.types import GroundingLevel, RunManifest, Transcript
from storyforge.kb.types import IngestReport
from storyforge.stages.knowledge import KnowledgeStage


class _FakeStore:
    def __init__(self, *, raise_on_ingest: bool = False) -> None:
        self.raise_on_ingest = raise_on_ingest

    def ingest(self, transcript: Transcript) -> IngestReport:
        if self.raise_on_ingest:
            raise KnowledgeBaseError("ingest down")
        return IngestReport(
            source_id=transcript.source.id,
            universe_id="u",
            content_hash="abc123",
            status="ingested",
            chunks_written=1,
            entities_new=0,
            entities_pending=0,
        )


def _ctx(tmp_path: Path) -> StageContext:
    settings = Settings(llm__api_key="test-key", workspace_dir=tmp_path / "ws")
    store = ArtifactStore(settings.workspace_dir, "proj")
    return StageContext(settings, store, RunManifest(project="proj"))


def _pending_path(tmp_path: Path) -> Path:
    return tmp_path / "ws" / "proj" / "03_knowledge" / "pending_ingest.jsonl"


def test_loose_ingest_failure_writes_pending_marker(
    tmp_path: Path, transcript: Transcript, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "storyforge.stages.knowledge.build_universe_store",
        lambda settings, universe: _FakeStore(raise_on_ingest=True),
    )
    stage = KnowledgeStage(transcripts=[transcript], universe="u", grounding=GroundingLevel.LOOSE)
    reports = stage.run(_ctx(tmp_path))
    assert reports == []
    assert _pending_path(tmp_path).exists()


def test_strict_ingest_failure_raises(
    tmp_path: Path, transcript: Transcript, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "storyforge.stages.knowledge.build_universe_store",
        lambda settings, universe: _FakeStore(raise_on_ingest=True),
    )
    stage = KnowledgeStage(transcripts=[transcript], universe="u", grounding=GroundingLevel.STRICT)
    with pytest.raises(KnowledgeBaseError):
        stage.run(_ctx(tmp_path))


def test_success_writes_reports(
    tmp_path: Path, transcript: Transcript, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "storyforge.stages.knowledge.build_universe_store",
        lambda settings, universe: _FakeStore(raise_on_ingest=False),
    )
    stage = KnowledgeStage(transcripts=[transcript], universe="u", grounding=GroundingLevel.LOOSE)
    reports = stage.run(_ctx(tmp_path))
    assert len(reports) == 1
    assert reports[0].status == "ingested"
    assert (tmp_path / "ws" / "proj" / "03_knowledge" / "reports.jsonl").exists()


# -- T4-DEV1: SearchHit.source_summary ----------------------------------------


class _FakePoint:
    """Minimal Qdrant point stand-in for ``_to_hit`` (no server needed)."""

    def __init__(self, *, payload: dict[str, object], score: float = 1.0, id: str = "p1") -> None:
        self.payload = payload
        self.score = score
        self.id = id


def test_to_hit_carries_source_summary() -> None:
    """T4-DEV1 AC: a hit carries source_summary when the payload has it."""
    from storyforge.kb.qdrant_store import QdrantKnowledgeStore

    store = object.__new__(QdrantKnowledgeStore)  # bypass __init__ (no server)
    point = _FakePoint(payload={"source_summary": "Bà Ngoại gánh hàng rong mùa đông."})
    hit = store._to_hit(point)  # type: ignore[attr-defined]
    assert hit.source_summary == "Bà Ngoại gánh hàng rong mùa đông."


def test_to_hit_source_summary_default_none() -> None:
    """Without a payload summary the field is None (not an error)."""
    from storyforge.kb.qdrant_store import QdrantKnowledgeStore

    store = object.__new__(QdrantKnowledgeStore)  # bypass __init__ (no server)
    point = _FakePoint(payload={})
    hit = store._to_hit(point)  # type: ignore[attr-defined]
    assert hit.source_summary is None


# -- M3-21 §8.3: allowed_licenses search gate ----------------------------------


def _memory_store(tmp_path: Path, allowed: list[str] | None = None) -> object:
    settings = Settings(
        llm__api_key="test-key",
        workspace_dir=tmp_path / "ws",
        knowledge__store="memory",
    )
    settings.knowledge.kb_data_dir = tmp_path / "kb"
    if allowed is not None:
        settings.knowledge.allowed_licenses = allowed
    from storyforge.kb.memory_store import build_memory_store

    return build_memory_store(settings, "u")


def _license_transcript(source_id: str, license: str) -> Transcript:
    from storyforge.core.types import SourceRef, TranscriptSegment

    return Transcript(
        source=SourceRef(id=source_id, license=license),  # type: ignore[arg-type]
        language="vi",
        audio_path=Path("fake.m4a"),
        segments=[TranscriptSegment(start=0.0, end=5.0, text=f"câu chuyện {source_id} đặc biệt")],
    )


def _search_sources(store: object) -> set[str]:
    from storyforge.kb.types import SearchIntent, SearchQuery

    hits = store.search(  # type: ignore[attr-defined]
        SearchQuery(text="câu chuyện", intent=SearchIntent.THEME, top_k=10)
    )
    return {h.source_id for h in hits}


def test_license_filter_excludes_disallowed_sources(tmp_path: Path) -> None:
    store = _memory_store(tmp_path, allowed=["cc0"])
    store.ingest(_license_transcript("src_cc0", "cc0"))  # type: ignore[attr-defined]
    store.ingest(_license_transcript("src_unknown", "unknown"))  # type: ignore[attr-defined]
    sources = _search_sources(store)
    assert "src_cc0" in sources
    assert "src_unknown" not in sources


def test_license_filter_empty_allows_all(tmp_path: Path) -> None:
    store = _memory_store(tmp_path)  # allowed_licenses=[] -> no gate
    store.ingest(_license_transcript("src_cc0", "cc0"))  # type: ignore[attr-defined]
    store.ingest(_license_transcript("src_unknown", "unknown"))  # type: ignore[attr-defined]
    sources = _search_sources(store)
    assert {"src_cc0", "src_unknown"} <= sources
