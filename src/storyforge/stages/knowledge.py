"""Stage 3 — ingest transcripts into the universe-scoped knowledge base.

The chunk/entity/embed write path lives in ``storyforge.kb.ingest`` (Dev 1);
this stage only loads transcripts, calls ``store.ingest()``, and persists the
``IngestReport`` + a jsonl snapshot. On failure in loose mode the transcript
stays DONE and the video pipeline proceeds; a ``pending_ingest`` marker is
written so ``storyforge ingest --drain`` can backfill later.
"""

from __future__ import annotations

from typing import Any

from storyforge.core.contracts import Stage, StageContext
from storyforge.core.exceptions import KnowledgeBaseError, StoryForgeError
from storyforge.core.logging import get_logger
from storyforge.core.types import GroundingLevel, Transcript
from storyforge.kb.types import IngestReport
from storyforge.providers.knowledge import build_universe_store

logger = get_logger(__name__)

_PENDING_INGEST = "pending_ingest.jsonl"


class KnowledgeStage(Stage):
    name = "knowledge"

    def __init__(
        self,
        transcripts: list[Transcript],
        universe: str,
        grounding: GroundingLevel,
    ) -> None:
        self.transcripts = transcripts
        self.universe = universe
        self.grounding = grounding

    def run(self, ctx: StageContext, *, force: bool = False) -> list[IngestReport]:
        store = build_universe_store(ctx.settings, self.universe)
        out_dir = ctx.store.dir("03_knowledge")

        reports: list[IngestReport] = []
        pending: list[dict[str, Any]] = []
        for transcript in self.transcripts:
            try:
                reports.append(store.ingest(transcript))
            except StoryForgeError as exc:
                if self.grounding is GroundingLevel.STRICT:
                    raise KnowledgeBaseError(
                        "indexing failed", details={"error": str(exc)}
                    ) from exc
                # Loose: transcript stays DONE, KB is deferred, video may proceed.
                pending.append({"source_id": transcript.source.id, "universe": self.universe})
                logger.warning(
                    "ingest deferred (loose)",
                    source_id=transcript.source.id,
                    error=str(exc),
                )

        ctx.store.write_jsonl(out_dir / "reports.jsonl", [r.model_dump() for r in reports])
        if pending:
            ctx.store.write_jsonl(out_dir / _PENDING_INGEST, pending)
        logger.info("knowledge ingested", ingested=len(reports), pending=len(pending))
        return reports
