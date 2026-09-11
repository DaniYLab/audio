"""Speech-to-text providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from storyforge.core.config import Settings
from storyforge.core.contracts import Transcriber
from storyforge.core.exceptions import ExternalServiceError, TranscriptionError
from storyforge.core.retry import retry_external
from storyforge.core.types import SourceRef, Transcript, TranscriptSegment


def build_transcriber(settings: Settings) -> Transcriber:
    engine = settings.transcription.engine
    if engine == "whisperx":
        return WhisperXTranscriber(settings)
    if engine == "assemblyai":
        return AssemblyAITranscriber(settings)
    raise TranscriptionError(f"unknown transcription engine: {engine}")


class WhisperXTranscriber:
    """Local WhisperX: fast batched whisper + word alignment + diarization.

    Heavy imports are deferred to __init__ so building the object in a stage-
    less context (tests, docs) stays cheap.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def transcribe(self, audio_path: str) -> Transcript:
        import whisperx  # type: ignore[import-untyped]

        cfg = self._settings.transcription
        try:
            device = cfg.device
            compute_type = cfg.compute_type if device == "cpu" else "float16"
            model = whisperx.load_model(cfg.model, device, compute_type=compute_type)
            audio = whisperx.load_audio(audio_path)
            result = model.transcribe(audio, batch_size=cfg.batch_size, language=cfg.language)

            segments = [
                TranscriptSegment(
                    start=float(seg["start"]),
                    end=float(seg["end"]),
                    text=str(seg["text"]).strip(),
                )
                for seg in result.get("segments", [])
            ]

            # Word-level alignment (Vietnamese default model is built in).
            align_model, metadata = whisperx.load_align_model(
                language_code=cfg.language, device=device
            )
            result = whisperx.align(result["segments"], align_model, metadata, audio, device)
            speakers = self._diarize(whisperx, audio, device, result)
            if speakers:
                for seg, speaker in zip(segments, speakers, strict=False):
                    seg.speaker = speaker
        except Exception as exc:
            raise TranscriptionError(
                f"whisperx failed for {audio_path}", details={"error": str(exc)}
            ) from exc

        return Transcript(
            source=SourceRef(id=Path(audio_path).stem),
            language=cfg.language,
            segments=segments,
            audio_path=Path(audio_path),
        )

    @staticmethod
    def _diarize(
        whisperx: Any, audio: Any, device: str, result: dict[str, Any]
    ) -> list[str] | None:
        """Optional speaker diarization; returns per-segment speaker labels."""
        try:
            diarize_model = whisperx.DiarizationPipeline(device=device)
            diarize_segments = diarize_model(audio)
            if diarize_segments is None:
                return None
            speakers = whisperx.assign_word_speakers(diarize_segments, result).get("speakers")
            return [str(s) for s in speakers] if speakers else None
        except Exception:
            return None  # diarization is best-effort; never fail transcription


class AssemblyAITranscriber:
    """Cloud STT via AssemblyAI (~$0.21/h, Vietnamese tier-1)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api_key = settings.transcription.assemblyai_api_key.get_secret_value()
        if not self._api_key:
            raise TranscriptionError("SF__TRANSCRIPTION__ASSEMBLYAI_API_KEY is required")

    def transcribe(self, audio_path: str) -> Transcript:
        import assemblyai  # type: ignore[import-not-found]

        assemblyai.settings.api_key = self._api_key
        cfg = self._settings.transcription

        def _call() -> Transcript:
            transcript = assemblyai.Transcriber().transcribe(
                audio_path, config=assemblyai.TranscriptionConfig(language_code=cfg.language)
            )
            if transcript.error:
                raise ExternalServiceError(
                    f"assemblyai error: {transcript.error}",
                    retryable=False,
                )
            segments = [
                TranscriptSegment(
                    start=float(s.start) / 1000.0,
                    end=float(s.end) / 1000.0,
                    text=(s.text or "").strip(),
                    speaker=s.speaker,
                )
                for s in transcript.segments or []
            ]
            return Transcript(
                source=SourceRef(id=Path(audio_path).stem),
                language=cfg.language,
                segments=segments,
                audio_path=Path(audio_path),
            )

        return retry_external(_call)
