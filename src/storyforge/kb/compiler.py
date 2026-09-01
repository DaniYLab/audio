"""BriefCompiler — turns raw KB search into a structured ``KnowledgeBrief``.

Two passes (KNOWLEDGE_BASE_DESIGN.md §4):
- ``build`` (pass 1, before outline): J1 theme search + J2 entity dossiers.
- ``update`` (pass 2, per beat): J3 scene palette.

The compiler holds episode-scoped citation state so a passage is never reused
across scenes, and it degrades cleanly in loose mode when the store is down.
"""

from __future__ import annotations

from storyforge.core.exceptions import KnowledgeBaseError
from storyforge.core.types import GroundingLevel, StoryBeat, StoryConfig
from storyforge.kb.types import (
    CitedPassage,
    EntityFacts,
    FactOrigin,
    KnowledgeBrief,
    KnowledgeStore,
    SearchHit,
    SearchIntent,
    SearchQuery,
)
from storyforge.ledger.store import LedgerStore

_SCENE_PALETTE_MAX = 4
_INVENTED_CAP = 6


class BriefCompiler:
    def __init__(
        self,
        store: KnowledgeStore,
        config: StoryConfig,
        ledger: LedgerStore | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._ledger = ledger
        self._cited_chunk_ids: set[str] = set()
        self._degraded = False
        self._degrade_reason: str | None = None

    def build(self) -> KnowledgeBrief:
        """Pass 1 — theme pack + entity dossiers for the whole cast."""
        if not self._store.health():
            self._fail_or_degrade(
                "knowledge store unavailable",
                KnowledgeBaseError("knowledge store unavailable"),
            )
            return KnowledgeBrief(degraded=True, reason=self._degrade_reason)

        brief = KnowledgeBrief()

        query_text = self._config.source_query or self._config.premise
        if query_text:
            hits = self._safe_search(
                SearchQuery(
                    text=query_text,
                    intent=SearchIntent.THEME,
                    group_by_source=True,
                    top_k=8,
                )
            )
            brief.theme = self._to_passages(hits, mark_cited=False)

        for name in self._cast_names():
            dossier = self._safe_query_entities(name)
            if dossier is None:
                brief.unknown_entities.append(name)
            else:
                brief.dossiers.append(dossier)
            # M3-W1: inject ledger facts for every character.
            self._inject_ledger_facts(brief, name)

        if self._degraded:
            brief.degraded = True
            brief.reason = self._degrade_reason
        return brief

    def update(self, brief: KnowledgeBrief, beat: StoryBeat) -> KnowledgeBrief:
        """Pass 2 — scene palette for one beat, with citation dedup."""
        if brief.degraded:
            return brief

        hits = self._safe_search(
            SearchQuery(
                text=self._scene_query(beat),
                intent=SearchIntent.SCENE,
                top_k=_SCENE_PALETTE_MAX,
            )
        )
        brief.palette = self._to_passages(hits, mark_cited=True)[:_SCENE_PALETTE_MAX]
        # M3-W1: refresh ledger facts for characters in this beat.
        for name in beat.characters:
            self._inject_ledger_facts(brief, name)

        if self._degraded:
            brief.degraded = True
            brief.reason = self._degrade_reason
        return brief

    # -- helpers -----------------------------------------------------------

    def _cast_names(self) -> list[str]:
        return [c.name for c in self._config.characters]

    def _scene_query(self, beat: StoryBeat) -> str:
        parts = [beat.summary]
        if beat.image_hint:
            parts.append(beat.image_hint)
        if beat.characters:
            parts.append(" ".join(beat.characters))
        return " ".join(parts)

    def _to_passages(self, hits: list[SearchHit], *, mark_cited: bool) -> list[CitedPassage]:
        passages: list[CitedPassage] = []
        for hit in hits:
            if mark_cited:
                if hit.chunk_id in self._cited_chunk_ids:
                    continue
                self._cited_chunk_ids.add(hit.chunk_id)
            passages.append(
                CitedPassage(
                    text=hit.text,
                    chunk_id=hit.chunk_id,
                    source_id=hit.source_id,
                    start_ts=hit.start_ts,
                )
            )
        return passages

    def _inject_ledger_facts(self, brief: KnowledgeBrief, name: str) -> None:
        """Pull ledger facts for ``name`` and split by origin (M3-W1)."""
        if self._ledger is None:
            return
        try:
            facts = self._ledger.query(subject=name)
        except Exception:
            return  # ledger unavailable — degrade silently
        for fact in facts:
            if fact.origin is FactOrigin.INVENTED:
                if len(brief.invented) < _INVENTED_CAP:
                    brief.invented.append(fact)
            else:
                brief.established.append(fact)

    def _safe_search(self, query: SearchQuery) -> list[SearchHit]:
        try:
            return self._store.search(query)
        except Exception as exc:
            self._fail_or_degrade("knowledge search failed", exc)
            return []

    def _safe_query_entities(self, name: str) -> EntityFacts | None:
        try:
            return self._store.query_entities(name)
        except Exception as exc:
            self._fail_or_degrade("entity query failed", exc)
            return None

    def _fail_or_degrade(self, reason: str, exc: Exception) -> None:
        if self._config.grounding is GroundingLevel.STRICT:
            raise KnowledgeBaseError(reason, details={"error": str(exc)}) from exc
        self._degraded = True
        self._degrade_reason = reason