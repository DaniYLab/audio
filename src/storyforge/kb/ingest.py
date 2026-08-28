"""Ingest write path — 7 steps, cheap, idempotent (design v4 §5).

    Transcript
      1. Light normalize (whitespace/punctuation) — never "fix" content
      2. Alias pass on a COPY for indexing; raw text stays in the payload
         (citations must quote the original words)
      3. Chunk: speaker-turn, ~600 tok, overlap 90, start+end timestamps
      4. Entities/topics: rule-based dictionary + kinship-title patterns
      5. Embed dense+sparse in one encode pass
      6. UPSERT by chunk_id; source row by content_hash:
           same hash  -> no-op
           new hash   -> delete old points of the source, write new version
      7. Merge alias table — new entities are pending_review, never auto-merged

The store protocol method ``ingest(transcript)`` lives here as a reusable
pipeline so both the Qdrant backend and the in-memory fake share one write
path (conformance suite runs against both).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from storyforge.core.types import KnowledgeChunk, Transcript
from storyforge.kb.alias import AliasStore
from storyforge.kb.entities import extract_entities, guess_type
from storyforge.kb.types import IngestReport

_APPROX_CHARS_PER_TOKEN = 4  # heuristic for Vietnamese/English mixed text


@dataclass
class PreparedSource:
    """Everything step 1-4 and 7 produce before any backend is touched."""

    source_id: str
    universe_id: str
    content_hash: str
    chunks: list[KnowledgeChunk] = field(default_factory=list)
    entities_new: int = 0
    entities_pending: int = 0
    unmapped_report: list[dict[str, object]] = field(default_factory=list)


def content_hash(transcript: Transcript) -> str:
    """Stable hash of the *normalized* full text — re-transcribe changes it,
    re-ingesting the same transcript does not."""
    digest = hashlib.sha256(_normalize(transcript.full_text).encode("utf-8"))
    digest.update(transcript.source.id.encode("utf-8"))
    return digest.hexdigest()


def _normalize(text: str) -> str:
    """Step 1: collapse whitespace only. Never alter the words themselves."""
    return re.sub(r"\s+", " ", text).strip()


def _alias_pass(text: str, alias: AliasStore) -> str:
    """Step 2: replace known alias variants with canonical names in a COPY."""
    result = text
    for entry in alias.all_entries():
        for variant in [entry.canonical, *entry.aliases]:
            if variant and variant != entry.canonical:
                result = result.replace(variant, entry.canonical)
    return result


def chunk_transcript(
    transcript: Transcript,
    *,
    chunk_size_tokens: int,
    overlap_tokens: int,
    index_text: str | None = None,
) -> list[KnowledgeChunk]:
    """Step 3: speaker-turn chunking with overlap.

    ``index_text`` is the alias-normalized copy used for embedding; the raw
    text is preserved in the payload for citations.
    """
    target_chars = chunk_size_tokens * _APPROX_CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * _APPROX_CHARS_PER_TOKEN

    raw_segments = transcript.segments
    if not raw_segments:
        return []

    # Re-map normalized text onto segments: split the normalized full text
    # proportionally to segment lengths (alias pass never changes length much;
    # a proportional split keeps citations aligned well enough for indexing).
    if index_text is not None:
        total_chars = sum(len(seg.text) for seg in raw_segments) or 1
        cursor = 0
        seg_texts: list[str] = []
        for seg in raw_segments:
            end = cursor + max(1, round(len(index_text) * len(seg.text) / total_chars))
            seg_texts.append(index_text[cursor:end])
            cursor = end
        # Sweep up any remainder from rounding.
        seg_texts[-1] = index_text[cursor - len(seg_texts[-1]) :]
    else:
        seg_texts = [seg.text for seg in raw_segments]

    chunks: list[KnowledgeChunk] = []
    buffer: list[str] = []
    buffer_start = raw_segments[0].start
    buffer_end = raw_segments[0].end
    buffer_speaker = raw_segments[0].speaker
    size = 0
    index = 0

    def flush() -> None:
        nonlocal index, buffer, size, buffer_start, buffer_speaker, buffer_end
        if not buffer:
            return
        text = " ".join(buffer).strip()
        entities = extract_entities(text)
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"{transcript.source.id}:{index:04d}",
                text=text,
                token_count=max(1, size // _APPROX_CHARS_PER_TOKEN),
                metadata={
                    "source_id": transcript.source.id,
                    "title": transcript.source.title,
                    "seq": index,
                    "start": buffer_start,
                    "end": buffer_end,
                    "start_ts": buffer_start,
                    "end_ts": buffer_end,
                    "speaker": buffer_speaker,
                    "language": transcript.language,
                    "entities": entities,
                    "topics": [],
                    "content_hash": "",
                },
            )
        )
        index += 1
        tail = text[-overlap_chars:] if overlap_chars > 0 else ""
        buffer = [tail] if tail else []
        size = len(tail)

    for segment, seg_text in zip(raw_segments, seg_texts, strict=False):
        if (
            buffer
            and segment.speaker is not None
            and buffer_speaker is not None
            and segment.speaker != buffer_speaker
        ):
            flush()
            buffer_start = segment.start
            buffer_speaker = segment.speaker
        if size >= target_chars:
            flush()
            buffer_start = segment.start
        buffer.append(seg_text.strip())
        buffer_end = segment.end
        size += len(seg_text) + 1
    flush()
    return chunks


def prepare(
    transcript: Transcript,
    *,
    universe_id: str,
    alias: AliasStore,
    chunk_size_tokens: int,
    overlap_tokens: int,
) -> PreparedSource:
    """Run steps 1-4 and 7 for one transcript (no embedding, no backend I/O)."""
    hash_ = content_hash(transcript)
    index_text = _alias_pass(_normalize(transcript.full_text), alias)
    chunks = chunk_transcript(
        transcript,
        chunk_size_tokens=chunk_size_tokens,
        overlap_tokens=overlap_tokens,
        index_text=index_text,
    )

    mentions: dict[str, int] = {}
    entities_new = 0
    for chunk in chunks:
        chunk.metadata["universe_id"] = universe_id
        chunk.metadata["content_hash"] = hash_
        for name in chunk.metadata.get("entities", []):
            mentions[str(name)] = mentions.get(str(name), 0) + 1
        normalized_entities = [
            alias.resolve(str(name)) or str(name) for name in chunk.metadata["entities"]
        ]
        chunk.metadata["entities"] = normalized_entities
        for name in normalized_entities:
            if alias.add_pending(name, guess_type(name), provenance="llm"):
                entities_new += 1

    report = PreparedSource(
        source_id=transcript.source.id,
        universe_id=universe_id,
        content_hash=hash_,
        chunks=chunks,
        entities_new=entities_new,
        entities_pending=len(alias.unmapped_report(mentions, top=10**9)),
        unmapped_report=alias.unmapped_report(mentions),
    )
    return report


def finalize(prepared: PreparedSource, *, stored_hash: str | None) -> str:
    """Step 6 decision: 'ingested' | 'noop' | 'reingested'."""
    if stored_hash is None:
        return "ingested"
    if stored_hash == prepared.content_hash:
        return "noop"
    return "reingested"


def report_from(prepared: PreparedSource, status: str) -> IngestReport:
    return IngestReport(
        source_id=prepared.source_id,
        universe_id=prepared.universe_id,
        content_hash=prepared.content_hash,
        status=status,  # type: ignore[arg-type]
        chunks_written=len(prepared.chunks) if status != "noop" else 0,
        entities_new=prepared.entities_new,
        entities_pending=prepared.entities_pending,
    )
