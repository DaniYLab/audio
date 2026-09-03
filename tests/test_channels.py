"""M7-W4: channel registry CRUD tests."""

from __future__ import annotations

from pathlib import Path

from storyforge.publish.channels import (
    ChannelRegistry,
    ChannelSpec,
    load_channel_registry,
    save_channel_registry,
)


def test_registry_add_and_remove() -> None:
    registry = ChannelRegistry()
    spec = ChannelSpec(platform="youtube", credentials_ref="env")
    assert registry.add(spec) is True
    assert registry.add(spec) is False  # duplicate rejected
    assert len(registry.channels) == 1
    assert registry.remove("youtube", "env") is True
    assert registry.remove("youtube", "env") is False
    assert registry.channels == []


def test_registry_multiple_platforms() -> None:
    registry = ChannelRegistry()
    registry.add(ChannelSpec(platform="youtube", credentials_ref="main"))
    registry.add(ChannelSpec(platform="shorts", credentials_ref="main"))
    registry.add(ChannelSpec(platform="tiktok", credentials_ref="main"))
    assert len(registry.channels) == 3
    # Same platform + ref is a duplicate; same ref different platform is not.
    assert registry.add(ChannelSpec(platform="youtube", credentials_ref="main")) is False


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    registry = ChannelRegistry()
    registry.add(ChannelSpec(platform="shorts", credentials_ref="main", vertical=True))
    path = tmp_path / "channels.yaml"
    save_channel_registry(registry, path)

    loaded = load_channel_registry(path)
    assert len(loaded.channels) == 1
    assert loaded.channels[0].platform == "shorts"
    assert loaded.channels[0].vertical is True
    assert loaded.channels[0].default_privacy == "private"


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    registry = load_channel_registry(tmp_path / "nope.yaml")
    assert registry.channels == []
