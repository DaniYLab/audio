"""M4-A4: YouTube metadata builder — title/description/tags from StoryConfig.

Usage::

    meta = build_metadata(config, episode=1, total=8, template="%{title} | Ep %{episode}")
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from storyforge.core.types import StoryConfig


class PublishMetadata(BaseModel):
    """Metadata for one YouTube video upload (A4 §1.2)."""

    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    category_id: str = "22"  # Entertainment
    privacy: Literal["private", "unlisted", "public"] = "private"
    make_for_kids: bool = False


def build_metadata(
    config: StoryConfig,
    episode: int,
    total: int,
    template: str = "%{title}",
) -> PublishMetadata:
    """Build the title/description/tags from the story config and a template.

    Template placeholders:
        ``%{title}``           — StoryConfig.title
        ``%{episode}``         — episode number (1-based)
        ``%{total}``           — total episodes in the season
        ``%{season}``           — season id (or "0")
        ``%{genre}``            — StoryConfig.genre
        ``%{premise}``          — StoryConfig.premise (first 200 chars)

    The title is truncated to 100 characters (YouTube limit).
    Description is built from: premise + characters + genre + tags line.
    Tags are derived from genre + character names + "storyforge".
    """
    title = _render_template(template, config, episode, total)[:100]
    description = _build_description(config, episode, total)
    tags = _build_tags(config)
    return PublishMetadata(title=title, description=description, tags=tags)


def _render_template(
    template: str, config: StoryConfig, episode: int, total: int
) -> str:
    replacements: dict[str, str] = {
        "%{title}": config.title,
        "%{episode}": str(episode),
        "%{total}": str(total),
        "%{season}": config.season or "0",
        "%{genre}": config.genre,
        "%{premise}": config.premise[:200],
    }
    result = template
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result.strip()


def _build_description(config: StoryConfig, episode: int, total: int) -> str:
    lines: list[str] = [f"Episode {episode}/{total}"]
    if config.premise:
        lines.append("")
        lines.append(config.premise)
    if config.characters:
        lines.append("")
        lines.append("Characters: " + ", ".join(c.name for c in config.characters))
    lines.append("")
    lines.append(f"Genre: {config.genre}")
    lines.append("")
    lines.append("Created with StoryForge.")
    return "\n".join(lines)


def _build_tags(config: StoryConfig) -> list[str]:
    tags: list[str] = [config.genre.lower(), "storyforge"]
    tags.extend(c.name.lower() for c in config.characters if c.name.lower() not in tags)
    seen = set()
    deduped: list[str] = []
    for tag in tags:
        t = tag.replace(" ", "").lower()
        if t not in seen:
            seen.add(t)
            deduped.append(tag)
    return deduped
