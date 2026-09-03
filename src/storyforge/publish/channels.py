"""M7-W4: Channel registry — multi-channel publish metadata.

Each channel spec describes a publishing destination (YouTube, TikTok, Shorts)
with its credentials ref and vertical-cut config. The publish stage (M7-V2)
reads this registry to decide which adapters to invoke.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ChannelSpec(BaseModel):
    """M7-W4 §2.2: a single publishing destination."""

    platform: Literal["youtube", "tiktok", "shorts"]
    credentials_ref: str  # key into the vault / env
    vertical: bool = False
    default_privacy: str = "private"


class ChannelRegistry(BaseModel):
    channels: list[ChannelSpec] = Field(default_factory=list)

    def add(self, spec: ChannelSpec) -> bool:
        """Add a channel (no duplicate platform+credentials_ref)."""
        if any(c.platform == spec.platform and c.credentials_ref == spec.credentials_ref for c in self.channels):
            return False
        self.channels.append(spec)
        return True

    def remove(self, platform: str, credentials_ref: str) -> bool:
        kept = [
            c
            for c in self.channels
            if not (c.platform == platform and c.credentials_ref == credentials_ref)
        ]
        changed = len(kept) != len(self.channels)
        self.channels = kept
        return changed


def save_channel_registry(registry: ChannelRegistry, path: Path | None = None) -> None:
    """Persist the registry to ``data/channels.yaml`` (atomic write)."""
    import yaml

    path = path or Path("data/channels.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.safe_dump(registry.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def load_channel_registry(path: Path | None = None) -> ChannelRegistry:
    """Load the channel registry from a YAML file."""
    path = path or Path("data/channels.yaml")
    if not path.exists():
        return ChannelRegistry()
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ChannelRegistry.model_validate(data)
