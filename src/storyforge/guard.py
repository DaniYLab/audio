"""Scene artifact guard — CheckpointDeltaGuard (M4-B3).

Port of the ainovel-cli guard: the only source of truth is the artifact on
disk, never text that "only exists in the LLM chat". StoryStage (and the
reviewer) must produce a *changed* artifact per scene; a writer that returns
the same output twice, or text with no matching on-disk change, is a failure.

Digest = sha256(narration_text + image_prompt). Duplicate digest within an
episode -> reject (the writer repeated itself).
"""

from __future__ import annotations

import hashlib

from storyforge.core.exceptions import StoryForgeError


class GuardError(StoryForgeError):
    """A scene failed to produce a distinct, on-disk artifact."""


class CheckpointDeltaGuard:
    def __init__(self) -> None:
        self._baseline: set[str] = set()  # digests recorded before generation
        self._seen: set[str] = set()  # digests produced during this run

    @staticmethod
    def digest(narration_text: str, image_prompt: str) -> str:
        raw = f"{narration_text}\u0000{image_prompt}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def add_baseline(self, digest: str) -> None:
        """Register a pre-existing artifact digest (before the stage runs)."""
        self._baseline.add(digest)
        self._seen.add(digest)

    def check(self, digest: str) -> None:
        """Accept a new scene digest; reject duplicates or unchanged output.

        Raises ``GuardError`` when the digest was already seen (either an old
        baseline artifact or a previous scene in this run) — the writer must
        return something genuinely new.
        """
        if digest in self._seen:
            raise GuardError(
                "scene produced an unchanged/duplicate artifact digest",
                details={"digest": digest[:16]},
            )
        self._seen.add(digest)


def guard_scene_digest(narration_text: str, image_prompt: str) -> str:
    return CheckpointDeltaGuard.digest(narration_text, image_prompt)
