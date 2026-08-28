"""Render a ``KnowledgeBrief`` into labeled prompt sections.

The prompt templates in ``prompts/`` reference these sections by placeholder;
this module is the only place that knows how to format them (KNOWLEDGE_BASE_DESIGN.md
§4.3). Established facts come from the fact ledger, which is a no-op in M1.
"""

from __future__ import annotations

from storyforge.kb.types import CitedPassage, EntityFacts


def render_theme(theme: list[CitedPassage]) -> str:
    if not theme:
        return "(no retrieved theme)"
    return "\n".join(f'- ({p.source_id}) "{p.text}"' for p in theme)


def render_dossiers(dossiers: list[EntityFacts]) -> str:
    if not dossiers:
        return "(no established dossiers)"
    blocks: list[str] = []
    for dossier in dossiers:
        sources = ", ".join(dossier.sources) or "no sources"
        facts = " · ".join(f.statement for f in dossier.facts) or "(no facts)"
        blocks.append(
            f"- {dossier.canonical} ({dossier.type}, {dossier.mention_count} mentions, {sources})\n"
            f"    facts: {facts}"
        )
        for conflict in dossier.conflicts:
            blocks.append(f"- CONFLICT: {conflict}")
    return "\n".join(blocks)


def render_unknown(unknown: list[str]) -> str:
    if not unknown:
        return "(none)"
    return "\n".join(f'- "{name}" không có trong corpus' for name in unknown)


def render_palette(palette: list[CitedPassage]) -> str:
    if not palette:
        return "(no scene palette)"
    lines: list[str] = []
    for p in palette:
        ts = f"{p.start_ts:.1f}" if p.start_ts is not None else "?"
        lines.append(f'- ({p.source_id} {ts}) "{p.text}"')
    return "\n".join(lines)


def render_established(facts: list[str]) -> str:
    if not facts:
        return "(no established facts)"
    return "\n".join(f"- {fact}" for fact in facts)


def render_degraded(reason: str | None) -> str:
    return f"[KB DEGRADED: {reason or 'knowledge base unavailable'} — viết từ premise]"
