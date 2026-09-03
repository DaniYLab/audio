"""M6-W2: music mood auto-classification fallbacks (no LLM calls)."""

from __future__ import annotations

from storyforge.core.config import Settings
from storyforge.core.types import StoryConfig, StoryStyle


def _settings(api_key: str = "") -> Settings:
    return Settings(llm__api_key=api_key)


def test_classify_mood_no_api_key_falls_back_calm() -> None:
    """Without an LLM key the classifier degrades to ``calm`` (M6-W2)."""
    from storyforge.musicmood import classify_mood

    story = _story()
    mood = classify_mood(story, _settings(api_key=""), available_moods=["calm", "tense"])
    assert mood == "calm"


def test_classify_mood_no_available_moods_returns_none() -> None:
    """Empty mood whitelist → no music (None)."""
    from storyforge.musicmood import classify_mood

    mood = classify_mood(_story(), _settings(), available_moods=[])
    assert mood is None


def test_classify_mood_fallback_first_available_without_calm() -> None:
    """No calm available → the first whitelisted mood wins."""
    from storyforge.musicmood import classify_mood

    mood = classify_mood(_story(), _settings(), available_moods=["dark", "joyful"])
    assert mood == "dark"


def _story():
    from storyforge.core.types import Story

    return Story(
        config=StoryConfig(
            title="T", genre="test", universe="u", language="vi", style=StoryStyle()
        ),
        outline=[],
        scenes=[],
    )
