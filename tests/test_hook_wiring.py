"""M4-A1: hook config pipe (settings.story.hook → StoryStage)."""

from __future__ import annotations

from storyforge.core.config import Settings
from storyforge.core.types import StoryBeat, StoryConfig, StoryStyle


def _config(**kw: object) -> StoryConfig:
    return StoryConfig(
        title="T",
        genre="test",
        universe="u",
        target_minutes=1,
        language="vi",
        style=StoryStyle(),
        **kw,  # type: ignore[arg-type]
    )


def _original() -> StoryBeat:
    return StoryBeat(
        beat_id="hook_00",
        summary="Original hook.",
        characters=["Lan"],
        image_hint="a rainy day",
    )


def _fake_llm_maker(monkeypatch, response: str) -> None:
    """Monkeypatch the LLMClient used by m4tools to return a fixed response."""
    import storyforge.m4tools as m4

    class _FakeLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def chat(self, system: str, user: str) -> str:
            return response

    monkeypatch.setattr(m4, "LLMClient", _FakeLLM)


def test_choose_hook_beat_manual_returns_none():
    from storyforge.m4tools import choose_hook_beat

    settings = Settings(llm__api_key="test-key")
    result = choose_hook_beat(settings, _config(), "manual", _original())
    assert result is None


def test_choose_hook_beat_a_returns_variant(monkeypatch):
    _fake_llm_maker(
        monkeypatch,
        "BEAT: hook_00 | Variant A hook | Lan | a sunny day",
    )
    from storyforge.m4tools import choose_hook_beat

    settings = Settings(llm__api_key="test-key")
    result = choose_hook_beat(settings, _config(), "a", _original())
    assert result is not None
    assert result.beat_id == "hook_00"
    assert "Variant A" in result.summary


def test_choose_hook_beat_a_inherits_fallback_image_hint(monkeypatch):
    """When the variant line has no image hint, inherit from the original."""
    _fake_llm_maker(
        monkeypatch,
        "BEAT: hook_00 | Variant with no hint |  | ",
    )
    from storyforge.m4tools import choose_hook_beat

    settings = Settings(llm__api_key="test-key")
    original = _original()
    result = choose_hook_beat(settings, _config(), "a", original)
    assert result is not None
    assert result.image_hint == original.image_hint


def test_choose_hook_beat_auto_picks_higher_score(monkeypatch):
    """auto generates both variants and judges; the higher-scored one wins."""
    import storyforge.m4tools as m4

    def _fake_judge(settings: object, text: str) -> float:
        return 90.0 if "Variant B" in text else 70.0

    monkeypatch.setattr(m4, "_judge_hook", _fake_judge)

    def _fake_gen(writer, config, label):
        lines = {
            "a": "BEAT: hook_00 | Variant A | Lan | sunny",
            "b": "BEAT: hook_00 | Variant B | Lan | rainy",
        }
        return lines.get(label, lines["a"])

    monkeypatch.setattr(m4, "generate_hook_variant", _fake_gen)

    settings = Settings(llm__api_key="test-key")
    result = m4.choose_hook_beat(settings, _config(), "auto", _original())
    assert result is not None
    assert "Variant B" in result.summary  # score 90 > 70 → B wins


def test_choose_hook_beat_graceful_on_llm_error(monkeypatch):
    """LLM/parse failure does not crash the pipeline — returns None."""
    _fake_llm_maker(monkeypatch, "garbage response with no BEAT line")
    from storyforge.m4tools import choose_hook_beat

    settings = Settings(llm__api_key="test-key")
    result = choose_hook_beat(settings, _config(), "a", _original())
    assert result is None
