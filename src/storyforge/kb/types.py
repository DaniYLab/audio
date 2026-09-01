"""KB contract types — shared, frozen after day 2 of M1 (see WORKPLAN_M1.md).

Pure pydantic models + the ``KnowledgeStore`` protocol. No I/O, no backend
imports. Both the Qdrant backend (Dev 1) and the brief compiler (Dev 2)
depend on this module only — changes require a 15-minute sync + dual review.

Design source: docs/KNOWLEDGE_BASE_DESIGN.md (v4), section 9.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from storyforge.core.types import Transcript

# --- query & hits -----------------------------------------------------------


class SearchIntent(StrEnum):
    THEME = "theme"  # J1 — before outline, needs source coverage
    ENTITY = "entity"  # J2 — around one entity
    SCENE = "scene"  # J3 — per-beat, small passages at the right time


class MetadataFilters(BaseModel):
    language: str | None = None
    source_ids: list[str] | None = None
    topics: list[str] | None = None
    time_range: tuple[float, float] | None = None  # (start_ts, end_ts)
    min_transcript_quality: float | None = None


class SearchQuery(BaseModel):
    text: str
    intent: SearchIntent = SearchIntent.THEME
    top_k: int = 8
    candidate_k: int = 50  # reranker input size
    filters: MetadataFilters | None = None
    group_by_source: bool = False  # J1 enables; caps 2-3 hits/source
    use_reranker: bool = False


class SearchHit(BaseModel):
    chunk_id: str
    text: str  # raw transcript text, NOT alias-normalized
    score: float
    source_id: str
    start_ts: float
    end_ts: float
    speaker: str | None
    entities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


# --- entity dossier (J2) ----------------------------------------------------


class CitedPassage(BaseModel):
    text: str
    chunk_id: str  # citation back to the source chunk
    source_id: str
    start_ts: float | None = None


class EntityFactLine(BaseModel):
    statement: str  # one-line fact distilled from a chunk
    chunk_id: str
    source_id: str


class EntityFacts(BaseModel):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    type: str  # person | place | event | org | other
    mention_count: int = 0
    sources: list[str] = Field(default_factory=list)
    facts: list[EntityFactLine] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # never auto-resolved
    sample_passages: list[CitedPassage] = Field(default_factory=list)


class SourceRefKB(BaseModel):
    """Source-level summary returned by ``similar_sources``."""

    source_id: str
    title: str | None = None
    score: float = 0.0


# --- ingest -----------------------------------------------------------------


class IngestReport(BaseModel):
    source_id: str
    universe_id: str
    content_hash: str
    status: Literal["ingested", "noop", "reingested"]  # same hash = noop
    chunks_written: int
    entities_new: int  # pending_review — never auto-merged
    entities_pending: int
    # M2 §5.2: True when the per-source "notable moments" summary was
    # generated (or already present) during this ingest.
    summary_generated: bool = False
    # M3 §8.3: True when the source carries license=unknown — publish warning.
    license_warning: bool = False


# --- contract freeze (M3 §0 — approved by m3_design.md P1) -------------------
#
# The fact ledger is "the world of the written story", kept strictly apart
# from the KB ("the world of the sources"). Reviewer pass and compiler both
# consume these models; adding/removing fields = CR through ARCH.


class FactOrigin(StrEnum):
    CITED = "cited"  # traceable to a KB chunk (chunk_refs non-empty)
    INFERRED = "inferred"  # LLM interpretation of cited facts
    INVENTED = "invented"  # writer invention (allowed in loose mode)


class FactKind(StrEnum):
    CHARACTER = "character"
    EVENT = "event"
    SETTING = "setting"
    RELATION = "relation"
    ITEM = "item"


class Fact(BaseModel):
    """One established fact of the story world, per episode."""

    fact_id: str  # "f0001" — stable, readable, unique within a universe
    kind: FactKind
    subject: str  # normalized through the KB alias table (one table, two worlds)
    statement: str  # single simple declarative sentence, past tense
    origin: FactOrigin
    chunk_refs: list[str] = Field(default_factory=list)  # required when origin=CITED
    episode_id: str  # episode that established the fact
    scene_id: str | None = None
    superseded_by: str | None = None  # retcon pointer — never delete
    extracted_by: Literal["llm", "human", "facts_used"] = "llm"

    def model_post_init(self, __context: object) -> None:
        if self.origin is FactOrigin.CITED and not self.chunk_refs:
            raise ValueError("origin=cited requires non-empty chunk_refs")


class ConflictVerdict(StrEnum):
    NO_CONFLICT = "no_conflict"  # duplicate or complementary fact
    CONFLICT = "conflict"  # unintentional contradiction → fix the draft
    TWIST_OK = "twist_ok"  # beat declared intent=twist → supersede is valid


class ConflictReport(BaseModel):
    """Result of checking one candidate fact against the live ledger."""

    candidate: Fact
    conflicts: list[Fact] = Field(default_factory=list)  # live facts it hits
    verdict: ConflictVerdict = ConflictVerdict.NO_CONFLICT
    reason: str = ""


# --- writer-facing facade (NOT part of the store protocol) ------------------


class KnowledgeBrief(BaseModel):
    """Compiled retrieval context for the writer (built by BriefCompiler)."""

    theme: list[CitedPassage] = Field(default_factory=list)  # J1
    dossiers: list[EntityFacts] = Field(default_factory=list)  # J2
    palette: list[CitedPassage] = Field(default_factory=list)  # J3 — per-scene
    unknown_entities: list[str] = Field(default_factory=list)
    degraded: bool = False
    reason: str | None = None


# --- protocol ---------------------------------------------------------------


@runtime_checkable
class KnowledgeStore(Protocol):
    """Backend-agnostic knowledge store, bound to one universe at build time.

    Implementations MUST scope every operation to the universe they were
    built with — the universe filter is structural, not a query convention.

    ``transcript`` is typed via ``TYPE_CHECKING`` so this contract module
    stays free of core imports at runtime while implementations get exact
    parameter types.
    """

    def ingest(self, transcript: Transcript) -> IngestReport:
        """Index one transcript (normalize/chunk/embed happen inside)."""
        ...

    def search(self, query: SearchQuery) -> list[SearchHit]: ...

    def query_entities(self, name: str) -> EntityFacts | None:
        """Dossier for a canonical name or alias; None = unknown entity."""
        ...

    def similar_sources(self, source_id: str) -> list[SourceRefKB]: ...

    def health(self) -> bool: ...


__all__ = [
    "CitedPassage",
    "ConflictReport",
    "ConflictVerdict",
    "EntityFactLine",
    "EntityFacts",
    "Fact",
    "FactKind",
    "FactOrigin",
    "IngestReport",
    "KnowledgeBrief",
    "KnowledgeStore",
    "MetadataFilters",
    "SearchHit",
    "SearchIntent",
    "SearchQuery",
    "SourceRefKB",
]
