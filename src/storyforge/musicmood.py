"""M6-W2: MusicMoodClassifier — auto-select mood per episode from the scene
beats, using an optional LLM call (cheap, 1 per episode).

Fallback: ``calm`` if available, else no music. The CLI override (M4 A5)
beats the auto-classifier.
"""

from __future__ import annotations

from storyforge.core.config import Settings
from storyforge.core.types import Story


def _default_mood(available_moods: list[str]) -> str | None:
    return (
        "calm" if "calm" in available_moods else (available_moods[0] if available_moods else None)
    )


def classify_mood(
    story: Story,
    settings: Settings,
    available_moods: list[str] | None = None,
) -> str | None:
    """Return a mood for the episode, or ``None`` for no music.

    M6-W2: uses 1 LLM call (cheap) to read the beat summaries. When the
    classifier is not available (no LLM, no API key), falls back to ``calm``.
    """
    if not available_moods:
        return None
    if not settings.llm.api_key.get_secret_value():
        return _default_mood(available_moods)
    from storyforge.providers.llm import LLMClient, fill_prompt, load_prompt

    client = LLMClient(settings, settings.llm.reviewer_model)
    beats = [f"{b.beat_id}: {b.summary}" for b in story.outline]
    prompt = fill_prompt(
        load_prompt("outline"),
        {
            "language": story.config.language,
            "genre": story.config.genre,
            "tone": story.config.style.tone,
            "grounding": story.config.grounding.value,
            "title": story.config.title,
            "premise": story.config.premise,
            "characters": "\n".join(f"- {c.name}" for c in story.config.characters),
            "target_words": "0",
            "degraded": "",
            "theme": "",
            "dossiers": "",
            "established": "",
            "invented": "",
            "unknown": "",
        },
    )
    user = (
        f"Based on the beat summaries below, choose the most fitting music mood\n"
        f"from this whitelist: {', '.join(available_moods)}.\n"
        f"Respond with only the mood name, nothing else.\n\n"
        f"Beat summaries:\n" + "\n".join(beats)
    )
    try:
        response = client.chat(prompt, user)
        mood = response.strip().lower()
        if mood in available_moods:
            return mood
    except Exception:
        pass
    return _default_mood(available_moods)
