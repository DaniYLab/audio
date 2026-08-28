"""Central exception hierarchy.

Rules (see docs/CONVENTIONS.md):
- Every exception derives from ``StoryForgeError`` so callers can catch the
  whole family with one clause.
- Exceptions carry machine-readable context (``details``) for structured logs;
  human messages stay short and actionable.
- Stage failures raise ``StageFailedError``; the orchestrator records them in
  the stage manifest and aborts the run — no silent partial success.
"""

from __future__ import annotations

from typing import Any


class StoryForgeError(Exception):
    """Base class for all StoryForge errors."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = details or {}


class ConfigError(StoryForgeError):
    """Invalid or missing configuration / secrets."""


class ExternalServiceError(StoryForgeError):
    """A remote provider (STT, LLM, TTS, image API) failed or timed out.

    ``retryable`` lets the retry policy distinguish transient network/API
    errors from deterministic ones (bad request, auth, quota).
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retryable = retryable


class DownloadError(StoryForgeError):
    """Failed to download source audio (network, extraction, ToS guard)."""


class TranscriptionError(StoryForgeError):
    """Failed to transcribe audio."""


class KnowledgeBaseError(StoryForgeError):
    """Failed to index or query the knowledge base."""


class StoryGenerationError(StoryForgeError):
    """LLM story generation failed or produced unusable output."""


class TTSError(StoryForgeError):
    """Failed to synthesize narration audio."""


class ImageGenerationError(StoryForgeError):
    """Failed to generate illustration images."""


class VideoAssemblyError(StoryForgeError):
    """FFmpeg assembly failed."""


class StageFailedError(StoryForgeError):
    """A pipeline stage failed; contains the originating stage name."""

    def __init__(self, stage_name: str, cause: StoryForgeError) -> None:
        super().__init__(f"stage '{stage_name}' failed: {cause}", details=cause.details)
        self.stage_name = stage_name
        self.cause = cause


class WorkspaceError(StoryForgeError):
    """Artifact workspace is missing, locked, or corrupted."""
