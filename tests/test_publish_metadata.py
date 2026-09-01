"""M4-A4: metadata builder unit tests (pure, no network)."""

from __future__ import annotations

from storyforge.core.types import CharacterSheet, StoryConfig
from storyforge.publish.metadata import PublishMetadata, build_metadata


def _config(**overrides: object) -> StoryConfig:
    values: dict[str, object] = {
        "title": "Câu chuyện mùa mưa",
        "genre": "drama",
        "universe": "test_universe",
        "premise": "Một cô bé học cách vượt qua nỗi sợ.",
        "characters": [
            CharacterSheet(
                name="Lan",
                appearance="girl with yellow raincoat",
                personality="curious",
            )
        ],
        "season": "s1",
    }
    values.update(overrides)
    return StoryConfig.model_validate(values)


def test_default_template_uses_title() -> None:
    meta = build_metadata(_config(), episode=1, total=8)
    assert isinstance(meta, PublishMetadata)
    assert meta.title == "Câu chuyện mùa mưa"


def test_template_placeholders() -> None:
    meta = build_metadata(
        _config(),
        episode=3,
        total=8,
        template="%{title} | Tập %{episode}/%{total} | %{season}",
    )
    assert meta.title == "Câu chuyện mùa mưa | Tập 3/8 | s1"


def test_genre_and_premise_placeholders() -> None:
    meta = build_metadata(
        _config(),
        episode=1,
        total=1,
        template="%{genre} — %{premise}",
    )
    assert meta.title.startswith("drama — Một cô bé")


def test_title_truncated_to_100_chars() -> None:
    long_title = "X" * 150
    meta = build_metadata(_config(title=long_title), episode=1, total=1)
    assert len(meta.title) <= 100


def test_tags_from_genre_and_characters() -> None:
    meta = build_metadata(_config(), episode=1, total=1)
    assert "drama" in meta.tags
    assert "lan" in meta.tags
    assert "storyforge" in meta.tags


def test_tags_deduplicated() -> None:
    meta = build_metadata(
        _config(genre="Drama", characters=[]), episode=1, total=1
    )
    assert meta.tags.count("drama") == 1


def test_privacy_default_private() -> None:
    meta = build_metadata(_config(), episode=1, total=1)
    assert meta.privacy == "private"
    assert meta.category_id == "22"
    assert meta.make_for_kids is False


def test_description_contains_episode_and_characters() -> None:
    meta = build_metadata(_config(), episode=2, total=6)
    assert "Episode 2/6" in meta.description
    assert "Lan" in meta.description
    assert "Genre: drama" in meta.description


def test_description_empty_premise_ok() -> None:
    meta = build_metadata(_config(premise=""), episode=1, total=1)
    assert "Episode 1/1" in meta.description
