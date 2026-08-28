"""Tests for artifact persistence and manifest resume behavior."""

from __future__ import annotations

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.types import RunManifest, StageStatus, Transcript


def test_manifest_roundtrip(store: ArtifactStore):
    manifest = RunManifest(project="test_project")
    manifest.mark("download", StageStatus.RUNNING)
    manifest.mark("download", StageStatus.DONE, sources=2)

    store.save_manifest(manifest)
    loaded = store.load_manifest()

    assert loaded.stages["download"].status is StageStatus.DONE
    assert loaded.stages["download"].metrics["sources"] == 2


def test_transcript_roundtrip(store: ArtifactStore, transcript: Transcript):
    path = store.transcript_path(transcript.source.id)
    store.write_model(path, transcript)

    loaded = store.read_model(path, Transcript)
    assert loaded.source.id == transcript.source.id
    assert len(loaded.segments) == len(transcript.segments)


def test_read_missing_artifact_raises(store: ArtifactStore):
    import pytest

    from storyforge.core.exceptions import WorkspaceError

    with pytest.raises(WorkspaceError):
        store.read_model(store.transcript_path("nope"), Transcript)
