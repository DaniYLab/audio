"""Qdrant knowledge store — the single production backend (design v4 §2.2, §6).

- Collection per universe, named vectors ``dense`` + ``sparse`` (BGE-M3).
- Payload index created BEFORE first ingest (source_id, speaker, start_ts,
  end_ts, entities, topics, language) so filters never scan.
- Hybrid search: Query API prefetch dense + sparse, fused with RRF.
- ``group_by_source`` caps hits per source (J1 theme searches need source
  diversity, not depth).
- The store is BOUND to one universe at construction; every read and write
  is structurally scoped — forgetting the filter is impossible.

Point IDs: Qdrant only accepts unsigned ints or UUIDs, while our stable
chunk ids look like ``ep01:0003``. Every chunk id is mapped to a
deterministic UUID5 and the original id rides in the payload, so
citations and idempotent upserts are unaffected.
"""

from __future__ import annotations

import uuid
from typing import Any

from storyforge.core.config import Settings
from storyforge.core.exceptions import KnowledgeBaseError
from storyforge.core.logging import get_logger
from storyforge.core.types import KnowledgeChunk, Transcript
from storyforge.kb.alias import AliasStore
from storyforge.kb.embedder import Embedding, build_embedder
from storyforge.kb.episode_summary import EpisodeSummaryStore
from storyforge.kb.ingest import finalize, prepare, report_from
from storyforge.kb.types import (
    CitedPassage,
    EntityFactLine,
    EntityFacts,
    IngestReport,
    SearchHit,
    SearchQuery,
    SourceRefKB,
)

logger = get_logger(__name__)

_DENSE_NAME = "dense"
_SPARSE_NAME = "sparse"
_MAX_PER_SOURCE = 3  # J1 group-by cap: 2-3 hits per source (design v4 §6)
_POINT_NAMESPACE = uuid.UUID("a3f5f1e0-0000-4000-8000-5f0d3b9c2a10")  # stable seed


def _point_id(chunk_id: str) -> str:
    """Deterministic UUID5 for a stable chunk id — Qdrant rejects free-form
    string ids, but upsert keyed by a stable UUID keeps idempotency."""
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


def _translate_error(exc: Exception) -> KnowledgeBaseError:
    """Map any Qdrant client error onto the KB exception contract (D7)."""
    message = str(exc).lower()
    retryable = any(
        token in message
        for token in ("timeout", "timed out", "connection", "unavailable", "refused", "429")
    )
    return KnowledgeBaseError(
        f"qdrant error: {exc}",
        details={"retryable": retryable, "backend": "qdrant"},
    )


class QdrantKnowledgeStore:
    """Universe-scoped Qdrant backend. Implements ``KnowledgeStore``."""

    def __init__(
        self,
        settings: Settings,
        universe_id: str,
        embedder: Any = None,
        reranker: Any = None,
        summarizer: Any = None,
    ) -> None:
        from qdrant_client import QdrantClient  # lazy heavy import

        self._settings = settings
        self._universe_id = universe_id
        self._kb = settings.knowledge
        self._client = QdrantClient(
            url=settings.knowledge.qdrant_url,
            api_key=settings.knowledge.qdrant_api_key.get_secret_value() or None,
            timeout=int(settings.knowledge.qdrant_timeout_seconds),
        )
        # ``embedder``/``reranker``/``summarizer`` injection lets tests pass
        # deterministic fakes; defaults build the configured providers lazily
        # on first use so health() works without embedding credentials.
        self._embedder: Any = embedder
        self._injected_reranker = reranker
        self._reranker: Any = reranker
        self._summarizer: Any = summarizer
        self._alias = AliasStore(settings.knowledge.kb_data_dir / universe_id / "aliases.yaml")
        self._summaries = EpisodeSummaryStore(settings.knowledge.kb_data_dir)
        self._collection = f"{self._kb.collection_prefix}_{universe_id}"
        self._exists_cache: bool | None = None
        # The collection is created lazily at first write, sized to the
        # embedder's ACTUAL output dimension (the config value is a hint for
        # providers, not a guarantee).

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            self._embedder = build_embedder(self._settings)
        return self._embedder

    def _get_reranker(self) -> Any:
        if self._reranker is None:
            from storyforge.kb.reranker import build_reranker

            self._reranker = build_reranker(self._settings)
        return self._reranker

    # -- collection bootstrap (D1) --------------------------------------------

    def _collection_exists(self) -> bool:
        if self._exists_cache is None:
            self._exists_cache = self._client.collection_exists(self._collection)
        return self._exists_cache

    def _ensure_collection(self, dense_dim: int | None = None) -> None:
        from qdrant_client import models

        try:
            if self._collection_exists():
                return
            dim = dense_dim or self._kb.embedding.dimensions
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    _DENSE_NAME: models.VectorParams(
                        size=dim,
                        distance=models.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    _SPARSE_NAME: models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False)
                    ),
                },
            )
            # Payload indexes BEFORE any ingest (WORKPLAN D1).
            for field, schema in (
                ("source_id", models.PayloadSchemaType.KEYWORD),
                ("speaker", models.PayloadSchemaType.KEYWORD),
                ("language", models.PayloadSchemaType.KEYWORD),
                ("entities", models.PayloadSchemaType.KEYWORD),
                ("topics", models.PayloadSchemaType.KEYWORD),
                ("start_ts", models.PayloadSchemaType.FLOAT),
                ("end_ts", models.PayloadSchemaType.FLOAT),
            ):
                self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=schema,
                )
            logger.info("kb collection created", collection=self._collection, dim=dim)
            self._exists_cache = True
        except Exception as exc:
            raise _translate_error(exc) from exc

    # -- ingest (D3, step 6 lives here) ----------------------------------------

    def ingest(self, transcript: Transcript) -> IngestReport:
        try:
            prepared = prepare(
                transcript,
                universe_id=self._universe_id,
                alias=self._alias,
                chunk_size_tokens=self._kb.chunk_size_tokens,
                overlap_tokens=self._kb.chunk_overlap_tokens,
            )
            stored_hash = self._stored_hash(prepared.source_id)
            status = finalize(prepared, stored_hash=stored_hash)
            if status == "noop":
                return report_from(prepared, status)

            if status == "reingested":
                self._delete_source(prepared.source_id)

            embeddings = self._get_embedder().encode([c.text for c in prepared.chunks])
            self._ensure_collection(dense_dim=len(embeddings[0].dense) if embeddings else None)
            self._write_points(prepared.chunks, embeddings)
            self._alias.save()  # step 7: persist pending entities
            prepared.summary_generated = self._summarize_best_effort(transcript)
            self._stamp_license(prepared)
            logger.info(
                "kb ingest",
                source=prepared.source_id,
                status=status,
                chunks=len(prepared.chunks),
            )
            return report_from(prepared, status)
        except KnowledgeBaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    def _summarize_best_effort(self, transcript: Transcript) -> bool:
        """M2-V2: 1 LLM call per fresh source. Best-effort — a failed summary
        never fails the ingest (transcript is DONE regardless). Returns True
        when a summary was generated and stamped onto the chunk payload."""
        if self._summarizer is None and not self._kb.episode_summary:
            return False
        summarizer = self._summarizer
        if summarizer is None:
            from storyforge.kb.episode_summary import LLMEpisodeSummarizer

            summarizer = LLMEpisodeSummarizer(self._settings, self._universe_id)
        try:
            summary = summarizer.summarize(transcript)
            # The store owns universe stamping — a summarizer that guesses
            # the universe must not write outside this store's scope.
            if summary.universe_id != self._universe_id:
                summary = summary.model_copy(update={"universe_id": self._universe_id})
            self._summaries.save(summary)
            self._stamp_source_summary(transcript.source.id, summary)
            logger.info("episode summary saved", source=transcript.source.id)
            return True
        except Exception as exc:
            logger.warning(
                "episode summary failed (ingest continues)",
                source=transcript.source.id,
                error=str(exc),
            )
            return False

    def _stamp_source_summary(self, source_id: str, summary: object) -> None:
        """M2 §5.2: ride ``source_summary`` on every chunk of the source."""
        from qdrant_client import models

        from storyforge.kb.episode_summary import summary_text

        self._client.set_payload(
            collection_name=self._collection,
            payload={"source_summary": summary_text(summary)},  # type: ignore[arg-type]
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=source_id)
                        )
                    ]
                )
            ),
        )

    def _stamp_license(self, prepared: object) -> None:
        """M3 §8.3: record the source license on every chunk payload."""
        from qdrant_client import models

        source_id = prepared.source_id  # type: ignore[attr-defined]
        self._client.set_payload(
            collection_name=self._collection,
            payload={"license": prepared.license},  # type: ignore[attr-defined]
            points=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=source_id)
                        )
                    ]
                )
            ),
        )

    def _stored_hash(self, source_id: str) -> str | None:
        from qdrant_client import models

        if not self._collection_exists():
            return None
        response, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id))
                ]
            ),
            limit=1,
            with_payload=["content_hash"],
        )
        if not response:
            return None
        payload = response[0].payload or {}
        hash_ = payload.get("content_hash")
        return str(hash_) if hash_ else None

    def _delete_source(self, source_id: str) -> None:
        from qdrant_client import models

        self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=source_id)
                        )
                    ]
                )
            ),
        )

    def _write_points(self, chunks: list[KnowledgeChunk], embeddings: list[Embedding]) -> None:
        from qdrant_client import models

        points = []
        for chunk, embedding in zip(chunks, embeddings, strict=False):
            points.append(
                models.PointStruct(
                    id=_point_id(chunk.chunk_id),
                    vector={
                        _DENSE_NAME: embedding.dense,
                        _SPARSE_NAME: models.SparseVector(
                            indices=embedding.sparse.indices,
                            values=embedding.sparse.values,
                        ),
                    },
                    payload={
                        "chunk_id": chunk.chunk_id,
                        "universe_id": self._universe_id,
                        "text": chunk.text,
                        **chunk.metadata,
                    },
                )
            )
        if points:
            self._client.upsert(collection_name=self._collection, points=points)

    # -- search (D5) ------------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchHit]:
        from qdrant_client import models

        try:
            if not self._collection_exists():
                return []  # nothing ingested yet for this universe
            embedding = self._get_embedder().encode([query.text])[0]
            prefetch = [
                models.Prefetch(
                    query=embedding.dense,
                    using=_DENSE_NAME,
                    limit=query.candidate_k,
                    filter=self._build_filter(query),
                ),
                models.Prefetch(
                    query=models.SparseVector(
                        indices=embedding.sparse.indices,
                        values=embedding.sparse.values,
                    ),
                    using=_SPARSE_NAME,
                    limit=query.candidate_k,
                    filter=self._build_filter(query),
                ),
            ]
            fetched = self._client.query_points(
                collection_name=self._collection,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=query.candidate_k,
                with_payload=True,
            ).points

            hits = [self._to_hit(point) for point in fetched]
            # M2-V1: rerank the candidate pool BEFORE per-source capping and
            # top_k — otherwise group_by would cap pre-rerank noise.
            if query.use_reranker:
                hits = self._get_reranker().rerank(query.text, hits)
            if query.group_by_source:
                hits = _cap_per_source(hits, _MAX_PER_SOURCE)
            return hits[: query.top_k]
        except KnowledgeBaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    def _build_filter(self, query: SearchQuery) -> Any | None:
        """Structural universe scope + optional query filters.

        The universe condition is ALWAYS present — this is the tenancy
        boundary (design v4 §2.1), not a query convention.
        """
        from qdrant_client import models

        must: list[Any] = [
            models.FieldCondition(
                key="universe_id", match=models.MatchValue(value=self._universe_id)
            )
        ]
        # M3-21 §8.3: license gate — when the config restricts allowed licenses,
        # exclude chunks whose source license is not in the whitelist.
        allowed = self._kb.allowed_licenses
        if allowed:
            must.append(
                models.FieldCondition(
                    key="license", match=models.MatchAny(any=allowed)
                )
            )
        filters = query.filters
        if filters:
            if filters.language:
                must.append(
                    models.FieldCondition(
                        key="language", match=models.MatchValue(value=filters.language)
                    )
                )
            if filters.source_ids:
                must.append(
                    models.FieldCondition(
                        key="source_id", match=models.MatchAny(any=filters.source_ids)
                    )
                )
            if filters.topics:
                must.append(
                    models.FieldCondition(key="topics", match=models.MatchAny(any=filters.topics))
                )
            ranges: list[Any] = []
            if filters.time_range:
                start, end = filters.time_range
                ranges.append(
                    models.FieldCondition(key="start_ts", range=models.Range(gte=start, lte=end))
                )
        return models.Filter(must=must) if must else None

    def _to_hit(self, point: Any) -> SearchHit:
        payload = point.payload or {}
        return SearchHit(
            chunk_id=str(payload.get("chunk_id", point.id)),
            text=str(payload.get("text", "")),
            score=float(point.score),
            source_id=str(payload.get("source_id", "")),
            start_ts=float(payload.get("start_ts", 0.0)),
            end_ts=float(payload.get("end_ts", 0.0)),
            speaker=payload.get("speaker"),
            entities=[str(e) for e in payload.get("entities", [])],
            topics=[str(t) for t in payload.get("topics", [])],
            # T4-DEV1: expose the per-source summary (M2 §5.2) when present.
            source_summary=payload.get("source_summary"),
        )

    # -- entity dossier (J2) ------------------------------------------------------

    def query_entities(self, name: str) -> EntityFacts | None:
        canonical = self._alias.resolve(name)
        target = canonical or name
        try:
            hits = self.search(
                SearchQuery(
                    text=target,
                    top_k=50,
                    candidate_k=100,
                )
            )
        except KnowledgeBaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

        matching: list[SearchHit] = []
        lowered = target.lower()
        for hit in hits:
            hit_entities = [e.lower() for e in hit.entities]
            if lowered in hit_entities or lowered in hit.text.lower():
                matching.append(hit)
        if not matching:
            return None

        entry = self._alias.entry_for(name)
        facts: list[EntityFactLine] = []
        passages: list[CitedPassage] = []
        for hit in matching[:10]:
            facts.append(
                EntityFactLine(
                    statement=hit.text[:160],
                    chunk_id=hit.chunk_id,
                    source_id=hit.source_id,
                )
            )
            passages.append(
                CitedPassage(
                    text=hit.text,
                    chunk_id=hit.chunk_id,
                    source_id=hit.source_id,
                    start_ts=hit.start_ts,
                )
            )
        return EntityFacts(
            canonical=target,
            aliases=list(entry.aliases) if entry else [],
            type=entry.type if entry else "other",
            mention_count=len(matching),
            sources=sorted({h.source_id for h in matching}),
            facts=facts[:5],
            sample_passages=passages[:3],
        )

    def similar_sources(self, source_id: str) -> list[SourceRefKB]:
        """Sources whose average dense vector is closest to this one's."""
        try:
            hits = self.search(SearchQuery(text=source_id, top_k=100, candidate_k=100))
        except Exception as exc:
            raise _translate_error(exc) from exc
        by_source: dict[str, float] = {}
        counts: dict[str, int] = {}
        for hit in hits:
            if hit.source_id == source_id or not hit.source_id:
                continue
            by_source[hit.source_id] = by_source.get(hit.source_id, 0.0) + hit.score
            counts[hit.source_id] = counts.get(hit.source_id, 0) + 1
        ranked = sorted(
            (SourceRefKB(source_id=s, score=by_source[s] / counts[s]) for s in by_source),
            key=lambda r: -r.score,
        )
        return ranked

    # -- health (D7) -----------------------------------------------------------

    def health(self) -> bool:
        """True when the Qdrant server is reachable. A universe that has
        never been ingested is still healthy — its collection simply does
        not exist yet (created lazily at first write)."""
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False


def _cap_per_source(hits: list[SearchHit], cap: int) -> list[SearchHit]:
    per_source: dict[str, int] = {}
    capped: list[SearchHit] = []
    for hit in hits:
        count = per_source.get(hit.source_id, 0)
        if count < cap:
            capped.append(hit)
            per_source[hit.source_id] = count + 1
    return capped
