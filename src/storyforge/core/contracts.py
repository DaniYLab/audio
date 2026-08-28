"""Stage contracts.

Every pipeline stage implements ``Stage`` and declares ``name`` and
``output_dir``. Stages receive resolved ``Settings``, the ``ArtifactStore``,
and the current ``RunManifest``; they must:

- be idempotent: if their output artifact already exists and ``force=False``,
  record SKIPPED and return it (this is what makes runs resumable);
- raise a subclass of ``StoryForgeError`` on failure — never return None to
  signal an error;
- record cost/duration metrics on the manifest via ``context.mark_done``;
- do all I/O through the ArtifactStore, never ad-hoc paths.

Provider interfaces (Transcriber, TextToSpeech, ImageGenerator) follow the
same shape: a Protocol with a single ``generate``-style method, implemented
once per provider, selected in config.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.types import (
    Illustration,
    KnowledgeChunk,
    NarrationClip,
    RunManifest,
    SourceRef,
    StageStatus,
    Story,
    StoryConfig,
    StoryScene,
    Transcript,
)


class StageContext:
    """What the orchestrator hands to each stage."""

    def __init__(
        self,
        settings: Settings,
        store: ArtifactStore,
        manifest: RunManifest,
    ) -> None:
        self.settings = settings
        self.store = store
        self.manifest = manifest

    def mark_running(self, stage: str) -> None:
        self.manifest.mark(stage, StageStatus.RUNNING)

    def mark_done(self, stage: str, **metrics: object) -> None:
        self.manifest.mark(stage, StageStatus.DONE, **metrics)

    def mark_skipped(self, stage: str) -> None:
        self.manifest.mark(stage, StageStatus.SKIPPED)


class Stage(ABC):
    """Base class for pipeline stages."""

    name: str = "stage"

    @abstractmethod
    def run(self, ctx: StageContext, *, force: bool = False) -> object:
        """Execute the stage. Returns its primary output artifact(s)."""


@runtime_checkable
class Transcriber(Protocol):
    """Speech-to-text provider."""

    def transcribe(self, audio_path: str) -> Transcript: ...


@runtime_checkable
class TextToSpeech(Protocol):
    """TTS provider: synthesize one scene's narration to an audio file."""

    def synthesize(self, scene: StoryScene, out_path: str, voice: str | None) -> NarrationClip: ...


@runtime_checkable
class ImageGenerator(Protocol):
    """Image provider: render one scene illustration."""

    def generate(self, scene: StoryScene, out_path: str) -> Illustration: ...


# Re-export commonly used types so stage modules can import from one place.
__all__ = [
    "ArtifactStore",
    "Illustration",
    "ImageGenerator",
    "KnowledgeChunk",
    "NarrationClip",
    "RunManifest",
    "Settings",
    "SourceRef",
    "Stage",
    "StageContext",
    "Story",
    "StoryConfig",
    "StoryScene",
    "TextToSpeech",
    "Transcriber",
    "Transcript",
]
