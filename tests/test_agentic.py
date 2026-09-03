"""M7-W3: agentic loop tests — proposal generation (heuristic fallback) + approve."""

from __future__ import annotations

import json
from pathlib import Path

from storyforge.analytics.agentic import (
    approve_proposal,
    generate_proposals,
    list_proposals,
)
from storyforge.core.config import Settings


def _settings(tmp_path: Path) -> Settings:
    s = Settings(llm__api_key="test-key", workspace_dir=tmp_path / "ws")
    s.analytics.warehouse_dir = tmp_path / "analytics"
    return s


def _seed_project(
    tmp_path: Path, universe: str = "storyvu", video_id: str = "vid123"
) -> Path:
    """Create a project with a publish receipt + clips + low retention data."""
    project = tmp_path / "ws" / "ep1"
    (project / "07_video").mkdir(parents=True)
    (project / "05_tts").mkdir(parents=True)
    (project / "04_story").mkdir(parents=True)

    (project / "07_video" / "publish.json").write_text(
        json.dumps(
            {
                "project": "ep1",
                "video_id": video_id,
                "privacy": "private",
                "file_hash": "abc",
                "url": "https://youtu.be/vid123",
            }
        ),
        encoding="utf-8",
    )
    # A story artifact so the universe check passes.
    (project / "04_story" / "story.json").write_text(
        json.dumps(
            {
                "config": {"title": "T", "genre": "g", "universe": universe},
                "outline": [],
                "scenes": [],
            }
        ),
        encoding="utf-8",
    )
    # One narration clip sidecar so retention can map onto scenes.
    (project / "05_tts" / "hook_00_scene.json").write_text(
        json.dumps(
            {
                "scene_id": "hook_00_scene",
                "audio_path": "hook_00_scene.mp3",
                "duration_seconds": 30.0,
                "char_count": 60,
            }
        ),
        encoding="utf-8",
    )
    return project


def test_generate_proposals_heuristic(tmp_path: Path) -> None:
    """Low retention on a scene → one heuristic proposal written to disk."""
    settings = _settings(tmp_path)
    _seed_project(tmp_path)

    retention_dir = tmp_path / "analytics" / "retention"
    retention_dir.mkdir(parents=True)
    (retention_dir / "vid123.json").write_text(
        json.dumps(
            {
                "video_id": "vid123",
                "segments": [[0, 0.2], [50, 0.1], [100, 0.05]],
            }
        ),
        encoding="utf-8",
    )

    proposals = generate_proposals(settings, settings.analytics.warehouse_dir, tmp_path / "ws", "storyvu")
    assert len(proposals) == 1
    assert proposals[0].project == "ep1"
    assert proposals[0].change_kind in ("hook_rewrite", "pacing_cut")

    stored = list_proposals(settings.analytics.warehouse_dir, "ep1")
    assert len(stored) == 1


def test_generate_proposals_no_low_retention(tmp_path: Path) -> None:
    """Healthy retention → no proposals."""
    settings = _settings(tmp_path)
    _seed_project(tmp_path)

    retention_dir = tmp_path / "analytics" / "retention"
    retention_dir.mkdir(parents=True)
    (retention_dir / "vid123.json").write_text(
        json.dumps({"video_id": "vid123", "segments": [[0, 0.9], [100, 0.8]]}),
        encoding="utf-8",
    )

    proposals = generate_proposals(
        settings, settings.analytics.warehouse_dir, tmp_path / "ws", "storyvu", threshold=0.5
    )
    assert proposals == []


def test_approve_proposal_writes_decision(tmp_path: Path) -> None:
    """Approving a proposal appends to decisions/ (append-only, never applies)."""
    settings = _settings(tmp_path)
    _seed_project(tmp_path)
    retention_dir = tmp_path / "analytics" / "retention"
    retention_dir.mkdir(parents=True)
    (retention_dir / "vid123.json").write_text(
        json.dumps({"video_id": "vid123", "segments": [[0, 0.2], [100, 0.1]]}),
        encoding="utf-8",
    )
    proposals = generate_proposals(
        settings, settings.analytics.warehouse_dir, tmp_path / "ws", "storyvu", threshold=0.5
    )
    assert len(proposals) == 1

    approved = approve_proposal(
        settings.analytics.warehouse_dir, "ep1", proposals[0].created_at
    )
    assert approved is not None
    decisions = list((tmp_path / "analytics" / "decisions").glob("ep1.jsonl"))
    assert len(decisions) == 1
    assert "approved_at" in decisions[0].read_text(encoding="utf-8")


def test_approve_unknown_proposal_returns_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert approve_proposal(settings.analytics.warehouse_dir, "ep1", "bogus") is None
