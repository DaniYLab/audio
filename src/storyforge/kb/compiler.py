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
from storyforge.ctxpack import maybe_compact, store_summary_text
from storyforge.kb.episode_summary import EpisodeSummary, EpisodeSummaryStore
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
        compact_budget_tokens: int = 0,
        compact_keep_recent_episodes: int = 10,
        summary_store: EpisodeSummaryStore | None = None,
        ledger_as_of: str | None = None,  # M6-V2: as_of_episode for the writer view
    ) -> None:
        self._store = store
        self._config = config
        self._ledger = ledger
        self._compact_budget = compact_budget_tokens  # M6-W3: 0 = off
        self._compact_recent = compact_keep_recent_episodes
        # M6-W3: per-universe episode summaries feed the compacted store summary.
        self._summary_store = summary_store
        # M6-V2: facts visible to the writer are those valid at this episode.
        self._ledger_as_of = ledger_as_of
        self._cited_chunk_ids: set[str] = set()
        self._degraded = False
        self._degrade_reason: str | None = None

    def _compact_store_summary(self) -> str | None:
        """M6-W3: compact narrative summary from ledger + episode summaries.

        Built only from artifacts already on disk — no LLM call. Returns None
        when compaction is off or nothing is available (graceful degrade).
        """
        if self._compact_budget <= 0 or self._ledger is None:
            return None
        try:
            facts = self._ledger.query()
        except Exception:
            facts = []
        established = [f for f in facts if f.origin is not FactOrigin.INVENTED]
        invented = [f for f in facts if f.origin is FactOrigin.INVENTED]

        summaries: dict[str, EpisodeSummary] = {}
        if self._summary_store is not None:

            summary_dir = self._summary_store.root / self._config.universe / "summaries"
            if summary_dir.exists():
                for path in sorted(summary_dir.glob("*.json"))[-10:]:
                    summary = self._summary_store.load(self._config.universe, path.stem)
                    if summary is not None:
                        summaries[path.stem] = summary
        return store_summary_text(established, invented, summaries)

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
        return maybe_compact(
            brief,
            budget=self._compact_budget,
            recent_episodes=self._compact_recent,
            compiled_store_summary=self._compact_store_summary(),
        )

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
        return maybe_compact(
            brief,
            budget=self._compact_budget,
            recent_episodes=self._compact_recent,
            compiled_store_summary=self._compact_store_summary(),
        )

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
        """Pull ledger facts for ``name`` and split by origin (M3-W1).

        When ``ledger_as_of`` is set (M6-V2), only facts valid at that episode
        are visible — the writer never sees facts from the future.
        """
        if self._ledger is None:
            return
        try:
            facts = self._ledger.query(subject=name, as_of_episode=self._ledger_as_of)
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
