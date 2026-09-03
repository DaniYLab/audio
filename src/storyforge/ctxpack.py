"""M6-W3: ContextCompactor — compact the brief when serial episodes exceed the
LLM context budget.

Port of ainovel-cli StoreSummaryCompact. When the estimated token count of a
``KnowledgeBrief`` exceeds ``compact_when_over_tokens``, the compactor replaces
lower-priority sections (theme, dossier sample passages) with episode summaries
and a compact store summary built from the ledger — no new LLM call.
"""

from __future__ import annotations

from typing import Any

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


def store_summary_text(
    established: list[Any] | None = None,
    invented: list[Any] | None = None,
    summaries: dict[str, Any] | None = None,
    recent_episodes: int = 10,
) -> str:
    """Build a compact narrative summary from ledger facts + episode summaries
    (M6-W3 §2.5).  No new LLM call — the text is built from existing artifacts.

    Structured sections:
    - Established facts (characters, settings, events)
    - Recent events (last ``recent_episodes`` facts)
    - Sensory colour from the most recent episode summary (if available)

    Returns an empty string when nothing is available — the caller may still
    compact without it.
    """
    lines: list[str] = ["[COMPACTED STORE SUMMARY]"]

    if established:
        lines.append("")
        lines.append("ESTABLISHED:")
        for f in established[:5]:
            lines.append(f"  - {f.statement}")

    if invented:
        lines.append("")
        lines.append("INVENTED PREVIOUSLY:")
        for f in invented[:3]:
            lines.append(f"  - {f.statement}")

    if summaries:
        lines.append("")
        lines.append("RECENT MOMENTS:")
        for _, sm in list(summaries.items())[:recent_episodes]:
            if hasattr(sm, "moments") and sm.moments:
                last = sm.moments[-1]
                lines.append(f"  - {last.action} ({last.person}, {last.place})")
    return "\n".join(lines)


def maybe_compact(
    brief: KnowledgeBrief,
    budget: int,
    recent_episodes: int = 10,
    compiled_store_summary: str | None = None,
) -> KnowledgeBrief:
    """If ``budget > 0`` and the brief exceeds it, drop theme + sample passages
    and replace them with a compact summary note.

    Scenes after the current one keep their palette; the writer receives a
    ``<COMPACTED>`` note in the rendered prompt instead of the dropped sections.

    ``compiled_store_summary`` is the output of ``store_summary_text()`` built
    by the caller (BriefCompiler) which has access to the ledger and summaries
    store.
    """
    if budget <= 0 or _estimate_tokens(brief) <= budget:
        return brief

    brief.theme.clear()
    for dossier in brief.dossiers:
        dossier.sample_passages.clear()
    note = (
        f" [COMPACTED: brief exceeded {budget} tokens — "
        f"theme and sample passages removed;"
        f" {recent_episodes} recent episodes kept]"
    )
    if compiled_store_summary:
        note = "\n" + compiled_store_summary + "\n" + note
    brief.reason = (brief.reason or "") + note
    return brief
