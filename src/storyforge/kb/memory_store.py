"""In-memory knowledge store — the test double for the Qdrant backend.

Same protocol, same write path (it reuses kb.ingest.prepare), deterministic
scoring (bag-of-words overlap, no network, no model) so the conformance
suite and Dev 2's compiler tests can run anywhere in milliseconds.

One real backend + one test double > two real backends (design v4 §9).
"""

from __future__ import annotations

from collections import Counter

from storyforge.core.config import Settings
from storyforge.core.types import KnowledgeChunk, Transcript
from storyforge.kb.alias import AliasStore
from storyforge.kb.embedder import _terms
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


class InMemoryKnowledgeStore:
    """Universe-scoped fake. Implements ``KnowledgeStore``."""

    def __init__(self, settings: Settings, universe_id: str) -> None:
        self._settings = settings
        self._universe_id = universe_id
        self._kb = settings.knowledge
        # The alias table is real YAML on disk so the pending-merge step is
        # exercised; tests point kb_data_dir at tmp_path.
        self._alias = AliasStore(settings.knowledge.kb_data_dir / universe_id / "aliases.yaml")
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._source_hashes: dict[str, str] = {}

    # -- ingest ---------------------------------------------------------------

    def ingest(self, transcript: Transcript) -> IngestReport:
        prepared = prepare(
            transcript,
            universe_id=self._universe_id,
            alias=self._alias,
            chunk_size_tokens=self._kb.chunk_size_tokens,
            overlap_tokens=self._kb.chunk_overlap_tokens,
        )
        status = finalize(prepared, stored_hash=self._source_hashes.get(prepared.source_id))
        if status != "noop":
            if status == "reingested":
                self._chunks = {
                    cid: c
                    for cid, c in self._chunks.items()
                    if c.metadata.get("source_id") != prepared.source_id
                }
            for chunk in prepared.chunks:
                self._chunks[chunk.chunk_id] = chunk
            self._source_hashes[prepared.source_id] = prepared.content_hash
            self._alias.save()
        return report_from(prepared, status)

    # -- search ----------------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchHit]:
        query_terms = set(_terms(query.text))
        filters = query.filters

        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in self._chunks.values():
            meta = chunk.metadata
            if meta.get("universe_id") != self._universe_id:
                continue  # structural scope — the fake honors it too
            if filters:
                if filters.language and meta.get("language") != filters.language:
                    continue
                if filters.source_ids and meta.get("source_id") not in filters.source_ids:
                    continue
                if filters.time_range:
                    start, end = filters.time_range
                    if not (start <= float(meta.get("start_ts", 0.0)) <= end):
                        continue
            chunk_terms = set(_terms(chunk.text))
            overlap = len(query_terms & chunk_terms)
            if overlap == 0:
                continue
            score = overlap / (len(query_terms) + len(chunk_terms) + 1e-9)
            scored.append((score, chunk))

        scored.sort(key=lambda pair: (-pair[0], pair[1].chunk_id))
        hits = [
            SearchHit(
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                score=score,
                source_id=str(chunk.metadata.get("source_id", "")),
                start_ts=float(chunk.metadata.get("start_ts", 0.0)),
                end_ts=float(chunk.metadata.get("end_ts", 0.0)),
                speaker=chunk.metadata.get("speaker"),
                entities=[str(e) for e in chunk.metadata.get("entities", [])],
                topics=[str(t) for t in chunk.metadata.get("topics", [])],
            )
            for score, chunk in scored
        ]
        if query.group_by_source:
            per_source: dict[str, int] = {}
            capped: list[SearchHit] = []
            for hit in hits:
                count = per_source.get(hit.source_id, 0)
                if count < 3:
                    capped.append(hit)
                    per_source[hit.source_id] = count + 1
            hits = capped
        return hits[: query.top_k]

    # -- entity dossier (J2) ------------------------------------------------------

    def query_entities(self, name: str) -> EntityFacts | None:
        canonical = self._alias.resolve(name)
        target = canonical or name
        lowered = target.lower()
        matching = [
            chunk
            for chunk in self._chunks.values()
            if lowered in [str(e).lower() for e in chunk.metadata.get("entities", [])]
        ]
        if not matching:
            return None
        entry = self._alias.entry_for(name)
        facts = [
            EntityFactLine(
                statement=chunk.text[:160],
                chunk_id=chunk.chunk_id,
                source_id=str(chunk.metadata.get("source_id", "")),
            )
            for chunk in matching[:5]
        ]
        passages = [
            CitedPassage(
                text=chunk.text,
                chunk_id=chunk.chunk_id,
                source_id=str(chunk.metadata.get("source_id", "")),
                start_ts=float(chunk.metadata.get("start_ts", 0.0)) or None,
            )
            for chunk in matching[:3]
        ]
        return EntityFacts(
            canonical=target,
            aliases=list(entry.aliases) if entry else [],
            type=entry.type if entry else "other",
            mention_count=len(matching),
            sources=sorted({str(c.metadata.get("source_id", "")) for c in matching}),
            facts=facts,
            sample_passages=passages,
        )

    def similar_sources(self, source_id: str) -> list[SourceRefKB]:
        source_terms = Counter(
            term
            for chunk in self._chunks.values()
            if chunk.metadata.get("source_id") == source_id
            for term in _terms(chunk.text)
        )
        if not source_terms:
            return []
        scores: dict[str, float] = {}
        for chunk in self._chunks.values():
            other = str(chunk.metadata.get("source_id", ""))
            if other == source_id:
                continue
            chunk_terms = _terms(chunk.text)
            shared = sum(min(source_terms[t], chunk_terms.count(t)) for t in set(chunk_terms))
            if shared:
                scores[other] = scores.get(other, 0.0) + shared
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [SourceRefKB(source_id=s, score=v) for s, v in ranked]

    def health(self) -> bool:
        return True

    # -- test helpers (NOT part of the protocol) ------------------------------------

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def source_hash(self, source_id: str) -> str | None:
        return self._source_hashes.get(source_id)


def build_memory_store(settings: Settings, universe_id: str) -> InMemoryKnowledgeStore:
    return InMemoryKnowledgeStore(settings, universe_id)
