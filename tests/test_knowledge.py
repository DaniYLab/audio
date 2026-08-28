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
