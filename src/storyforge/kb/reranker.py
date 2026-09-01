"""Cross-encoder reranking — off by default, measured before it becomes one
(design v4 §6: "BGE-reranker-v2-m3 — cờ tắt, bật khi golden set có").

Stores call ``rerank(query, hits)`` only when ``SearchQuery.use_reranker`` is
set AND the store was built with a reranker (settings flag or injection).
The heavy CrossEncoder import stays inside the method (CONVENTIONS.md §1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from storyforge.core.config import Settings
from storyforge.core.logging import get_logger
from storyforge.kb.types import SearchHit

logger = get_logger(__name__)


@runtime_checkable
class Reranker(Protocol):
    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]: ...


class NoopReranker:
    """Default: pass hits through unchanged (flag exists, effect measured)."""

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        return hits


class CrossEncoderReranker:
    """BGE reranker v2-m3 via sentence-transformers CrossEncoder.

    Reranks by (query, passage) relevance; ties keep the retrieval order so
    results stay deterministic.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return hits
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

        model = CrossEncoder(self._model_name)
        pairs = [(query, hit.text) for hit in hits]
        scores = [float(s) for s in model.predict(pairs)]
        reranked = sorted(
            zip(scores, enumerate(hits), strict=True),
            key=lambda pair: (-pair[0], pair[1][0]),
        )
        result = [hit for _, (_, hit) in reranked]
        for hit, (_, (rank, _)) in zip(result, reranked, strict=True):
            hit.score = scores[rank]
        logger.debug("reranked", hits=len(hits))
        return result


def build_reranker(settings: Settings) -> Reranker:
    knowledge = settings.knowledge
    if knowledge.use_reranker:
        return CrossEncoderReranker(knowledge.reranker_model)
    return NoopReranker()


__all__ = ["CrossEncoderReranker", "NoopReranker", "Reranker", "build_reranker"]
