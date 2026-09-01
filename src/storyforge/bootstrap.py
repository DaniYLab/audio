"""Universe bootstrap from corpus (M4-B5) — port of the ainovel-cli
reverse-foundation import idea: turn an ingested KB into a StoryConfig draft
in one step, no manual config writing.

Flow:
    KB (entity dossiers + alias table + topics)
        -> collect person/place candidates (mention-filtered)
        -> ONE LLM call synthesizes premise + world rules + source_query
        -> StoryConfig draft YAML at data/kb/<universe>/bootstrap_draft.yaml
           (producer reviews/edits; nothing auto-runs)

Skipped entities (type != person/place, mention below threshold) are logged
in the draft so the producer sees what was dropped and why.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from storyforge.core.config import Settings
from storyforge.core.types import CharacterSheet, StoryConfig
from storyforge.kb.alias import AliasStore
from storyforge.kb.types import EntityFacts, KnowledgeStore

_MIN_MENTIONS = 2  # below this, an entity is too thin to bootstrap from
_MAX_CHARACTERS = 6  # cap so the premise call stays cheap


class BootstrapDraft(BaseModel):
    """The proposed StoryConfig seed + audit of what was skipped."""

    # universe_id is stamped by build_bootstrap_draft — the synthesizer does
    # not know it yet.
    universe_id: str = ""
    premise: str = ""
    world_rules: list[str] = Field(default_factory=list)
    source_query: str | None = None
    characters: list[CharacterSheet] = Field(default_factory=list)
    skipped_entities: list[str] = Field(default_factory=list)

    def to_story_config(
        self, *, title: str = "Bootstrap story", genre: str = "drama"
    ) -> StoryConfig:
        """Draft StoryConfig the producer can edit before any run."""
        return StoryConfig(
            title=title,
            genre=genre,
            universe=self.universe_id,
            premise=self.premise,
            source_query=self.source_query,
            characters=self.characters,
        )


Synthesizer = Callable[[list[EntityFacts]], "BootstrapDraft"]


def collect_candidates(
    store: KnowledgeStore, alias: AliasStore
) -> tuple[list[EntityFacts], list[str]]:
    """Person/place dossiers with enough mentions; the rest are skipped."""
    candidates: list[EntityFacts] = []
    skipped: list[str] = []
    for entry in alias.all_entries():
        if entry.type not in ("person", "place"):
            skipped.append(f"{entry.canonical} (type={entry.type})")
            continue
        dossier = store.query_entities(entry.canonical)
        if dossier is None:
            skipped.append(f"{entry.canonical} (no dossier)")
            continue
        if dossier.mention_count < _MIN_MENTIONS:
            skipped.append(f"{entry.canonical} (mentions={dossier.mention_count})")
            continue
        candidates.append(dossier)
    candidates.sort(key=lambda d: -d.mention_count)
    return candidates[:_MAX_CHARACTERS], skipped


def llm_synthesizer(settings: Settings) -> Synthesizer:
    """1 LLM call (writer model) to draft premise + world rules from dossiers."""

    def synthesize(dossiers: list[EntityFacts]) -> BootstrapDraft:
        from storyforge.providers.llm import LLMClient, fill_prompt, load_prompt

        client = LLMClient(settings, settings.llm.writer_model)
        template = load_prompt("bootstrap_universe")
        dossier_text = "\n\n".join(
            f"### {d.canonical} ({d.type}, {d.mention_count} mentions)\n"
            + "\n".join(f"- {f.statement}" for f in d.facts[:6])
            for d in dossiers
        )
        response = client.chat(
            system=fill_prompt(template, {"language": settings.llm.base_url or "vi"}),
            user=fill_prompt(template, {"dossiers": dossier_text}),
        )
        # Deterministic fallback parsing: lines after markers.
        premise = _section(response, "PREMISE:")
        world = [
            line.strip("- ")
            for line in _section(response, "WORLD RULES:").splitlines()
            if line.strip()
        ]
        query = _section(response, "SOURCE QUERY:")
        return BootstrapDraft(premise=premise, world_rules=world, source_query=query or None)

    return synthesize


def build_bootstrap_draft(
    store: KnowledgeStore,
    alias: AliasStore,
    universe_id: str,
    *,
    settings: Settings | None = None,
    synthesizer: Synthesizer | None = None,
) -> BootstrapDraft:
    """Collect candidates + one synthesis call; characters from dossiers."""
    dossiers, skipped = collect_candidates(store, alias)
    synth = synthesizer or (llm_synthesizer(settings) if settings else None)
    if synth is None:
        raise ValueError("bootstrap needs settings or an injected synthesizer")

    draft = synth(dossiers)
    draft.universe_id = universe_id
    draft.skipped_entities = skipped
    draft.characters = [
        CharacterSheet(
            name=d.canonical,
            appearance="",  # producer fills in; dossiers carry facts, not looks
            personality=" ".join(f.statement for f in d.facts[:3]),
            speech_style=None,
        )
        for d in dossiers
        if d.type == "person"
    ]
    return draft


def write_draft(universe_dir: Path, draft: BootstrapDraft) -> Path:
    """Persist the draft YAML for producer review (AC3, never auto-run)."""
    import yaml

    out = universe_dir / "bootstrap_draft.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump(draft.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return out


def _section(text: str, marker: str) -> str:
    """Text after ``marker`` up to the next blank/marker line (best-effort)."""
    idx = text.find(marker)
    if idx == -1:
        return ""
    rest = text[idx + len(marker) :].strip()
    for stop in ("PREMISE:", "WORLD RULES:", "SOURCE QUERY:"):
        end = rest.find(stop)
        if end != -1:
            rest = rest[:end]
            break
    return rest.strip()
