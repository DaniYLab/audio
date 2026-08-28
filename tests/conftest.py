"""Shared pytest fixtures. Unit tests never touch network or heavy models."""

from __future__ import annotations

import os

# Settings loads at import time and LLMSettings requires an API key; tests
# never call the LLM, so a placeholder key keeps collection working without
# a real .env.
os.environ.setdefault("SF__LLM__API_KEY", "test-key")

from pathlib import Path

import pytest

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.types import (
    CharacterSheet,
    SourceRef,
    StoryBeat,
    StoryConfig,
    StoryScene,
    Transcript,
    TranscriptSegment,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture()
def store(workspace: Path) -> ArtifactStore:
    return ArtifactStore(workspace, "test_project")


@pytest.fixture()
def transcript() -> Transcript:
    return Transcript(
        source=SourceRef(id="abc123", title="Sample podcast", duration_seconds=120),
        language="vi",
        audio_path=Path("fake.m4a"),
        segments=[
            TranscriptSegment(start=0.0, end=5.0, text="Xin chào các bạn.", speaker="SPEAKER_00"),
            TranscriptSegment(
                start=5.0, end=10.0, text="Hôm nay tôi sẽ kể một câu chuyện.", speaker="SPEAKER_00"
            ),
            TranscriptSegment(
                start=10.0, end=15.0, text="Câu chuyện xảy ra rất lâu rồi.", speaker="SPEAKER_01"
            ),
        ],
    )


@pytest.fixture()
def story_config() -> StoryConfig:
    return StoryConfig(
        title="Test Story",
        genre="test",
        universe="test_universe",
        target_minutes=1,
        language="vi",
        premise="A test premise.",
        characters=[
            CharacterSheet(
                name="Lan",
                appearance="Vietnamese girl, yellow raincoat",
                personality="curious",
            )
        ],
    )


@pytest.fixture()
def scene(story_config: StoryConfig) -> StoryScene:
    beat = StoryBeat(
        beat_id="beat_00",
        summary="Lan walks through the market.",
        characters=["Lan"],
        image_hint="a girl in a yellow raincoat at a rainy market",
    )
    return StoryScene(
        scene_id="beat_00_scene",
        beat=beat,
        narration_text="Lan bước qua con chợ trong mưa.",
        image_prompt="watercolor. Lan: Vietnamese girl, yellow raincoat. rainy market",
    )
