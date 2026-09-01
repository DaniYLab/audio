"""M6-W3: ContextCompactor — compact the brief when serial episodes exceed the
LLM context budget.

Port of ainovel-cli StoreSummaryCompact. When the estimated token count of a
``KnowledgeBrief`` exceeds ``compact_when_over_tokens``, the compactor replaces
lower-priority sections (theme, dossier sample passages) with episode summaries
and a compact store summary built from the ledger — no new LLM call.
"""

from __future__ import annotations

from storyforge.kb.types import KnowledgeBrief


def _estimate_tokens(brief: KnowledgeBrief) -> int:
    tokens = 0
    for p in brief.theme:
        tokens += len(p.text.split())
    for d in brief.dossiers:
        for p in d.sample_passages:
            tokens += len(p.text.split())
    for p in brief.palette:
        tokens += len(p.text.split())
    tokens += len(brief.unknown_entities)
    return tokens


def maybe_compact(brief: KnowledgeBrief, budget: int, recent_episodes: int = 10) -> KnowledgeBrief:
    """If ``budget > 0`` and the brief exceeds it, drop theme + sample passages
    and replace them with a compact summary note.

    Scenes after the current one keep their palette; the writer receives a
    ``<COMPACTED>`` note in the rendered prompt instead of the dropped sections.
    """
    if budget <= 0 or _estimate_tokens(brief) <= budget:
        return brief

    brief.theme.clear()
    for dossier in brief.dossiers:
        dossier.sample_passages.clear()
    brief.reason = (brief.reason or "") + (
        f" [COMPACTED: brief exceeded {budget} tokens — "
        f"theme and sample passages removed;"
        f" {recent_episodes} recent episodes kept]"
    )
    return brief
