"""M6/M7 DEV2 unit tests — ctxpack, musicmood, cuts, channels, agentic.

Pure logic, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from storyforge.analytics.agentic import RetentionProposal, RetentionProposalList
from storyforge.ctxpack import maybe_compact
from storyforge.kb.types import KnowledgeBrief
from storyforge.publish.channels import ChannelRegistry, ChannelSpec, load_channel_registry
from storyforge.publish.cuts import VerticalCutConfig

# --- M6-W3: ctxpack ------------------------------------------------------------


def test_ctxpack_budget_under_threshold():
    brief = KnowledgeBrief()
    result = maybe_compact(brief, budget=1000)
    assert result == brief  # no change under budget


def test_ctxpack_compacts_when_over():
    brief = KnowledgeBrief(
        theme=[{"text": "x " * 500, "chunk_id": "c1", "source_id": "s1"}],
    )
    result = maybe_compact(brief, budget=100)
    assert not result.theme  # theme cleared
    assert "COMPACTED" in (result.reason or "")


# --- M6-W2: musicmood (no LLM — fallback path) --------------------------------


def test_musicmood_fallback_no_llm():
    from storyforge.core.config import Settings

    settings = Settings(llm__api_key="test-key")
    from storyforge.core.types import Story, StoryBeat, StoryConfig

    story = Story(
        config=StoryConfig(title="t", genre="g", universe="u"),
        outline=[StoryBeat(beat_id="b1", summary="s")],
        scenes=[],
    )
    from storyforge.musicmood import classify_mood

    mood = classify_mood(story, settings, available_moods=["calm", "tense"])
    assert mood in ("calm", "tense")  # fallback to first available


# --- M7-W2: vertical cut config -------------------------------------------------


def test_vertical_cut_config_default():
    cfg = VerticalCutConfig()
    assert cfg.width == 1080
    assert cfg.height == 1920
    assert cfg.hook_full_frame is True


# --- M7-W4: channel registry ----------------------------------------------------


def test_channel_registry_roundtrip(tmp_path: Path):
    path = tmp_path / "channels.yaml"
    reg = ChannelRegistry(channels=[ChannelSpec(platform="youtube", credentials_ref="yt")])
    import yaml

    path.write_text(yaml.safe_dump(reg.model_dump(mode="json")), encoding="utf-8")
    loaded = load_channel_registry(path)
    assert len(loaded.channels) == 1
    assert loaded.channels[0].platform == "youtube"


def test_channel_registry_empty(tmp_path: Path):
    loaded = load_channel_registry(tmp_path / "nonexistent.yaml")
    assert loaded.channels == []


# --- M7-W3: agentic proposals ----------------------------------------------------


def test_retention_proposal_validation():
    prop = RetentionProposal(
        project="demo",
        scene_id="hook_00",
        dimension="hook",
        change_kind="hook_rewrite",
        change="New hook text",
        reason="Retention < 40%",
        expected_impact="+5%",
    )
    assert prop.dimension == "hook"

    with pytest.raises(ValidationError):
        RetentionProposal(dimension="unknown")  # missing required fields


def test_retention_proposal_list():
    lst = RetentionProposalList()
    assert lst.proposals == []
