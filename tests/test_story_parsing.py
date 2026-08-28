"""Tests for LLM response parsing — pure, no network."""

from __future__ import annotations

import pytest

from storyforge.core.exceptions import StoryGenerationError
from storyforge.providers.llm import StoryWriter


def test_parse_beats_valid():
    response = """
BEAT: beat_01 | Lan arrives at the market | Lan | a girl at a rainy market entrance
BEAT: beat_02 | She meets her grandmother | Lan, Bà Ngoại |
"""
    beats = StoryWriter._parse_beats(response)
    assert len(beats) == 2
    assert beats[0].beat_id == "beat_01"
    assert beats[0].characters == ["Lan"]
    assert beats[1].image_hint == ""


def test_parse_beats_invalid_raises():
    with pytest.raises(StoryGenerationError):
        StoryWriter._parse_beats("no beats here at all")


def test_parse_scene_response():
    response = "Lan bước đi trong mưa.\n\nIMAGE_PROMPT: watercolor, girl in yellow raincoat"
    narration, prompt = StoryWriter._parse_scene_response(response)
    assert "Lan" in narration
    assert prompt.startswith("watercolor")


def test_parse_scene_missing_prompt_raises():
    with pytest.raises(StoryGenerationError):
        StoryWriter._parse_scene_response("just narration, no prompt")
