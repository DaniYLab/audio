"""Stage 2 — speech-to-text.

Provider selection lives in config (SF__TRANSCRIPTION__ENGINE). The stage is
idempotent per source: an existing transcript artifact short-circuits the
expensive transcription call.
"""

from __future__ import annotations

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import TranscriptionError
from storyforge.core.logging import get_logger
from storyforge.core.types import SourceRef, Transcript

logger = get_logger(__name__)


class TranscribeStage(Stage):
    name = "transcribe"

    def __init__(self, sources: list[SourceRef]) -> None:
        self.sources = sources

    def run(self, ctx: StageContext, *, force: bool = False) -> list[Transcript]:
        from storyforge.providers.stt import build_transcriber

        transcriber = build_transcriber(ctx.settings)
        transcripts: list[Transcript] = []
        for source in self.sources:
            if source.local_path is None:
                raise TranscriptionError(f"source {source.id} has no local audio file")
            out_path = ctx.store.transcript_path(source.id)
            if out_path.exists() and not force:
                logger.info("transcript exists, skipping", source=source.id)
                transcripts.append(ctx.store.read_model(out_path, Transcript))
                continue

            logger.info("transcribing", source=source.id, engine=ctx.settings.transcription.engine)
            transcript = transcriber.transcribe(str(source.local_path))
            transcript.source = source
            ctx.store.write_model(out_path, transcript)
            transcripts.append(transcript)
        return transcripts
