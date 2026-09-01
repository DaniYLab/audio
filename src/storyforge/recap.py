"""Previously-On Recap (M4-A2) — 20-30s catch-up clip for episodes >= 2.

Source of truth: the FACT LEDGER (established facts of the story world) +
per-source EpisodeSummaries (notable moments of the source audio). NOT the
full story text — a recap must summarize canon, not re-narrate.

Flow:
    ledger facts (recent) + summaries
        -> recap script (20-30s, ~60-90 words)        [DEV1 deterministic]
        -> TTS clip                                   [TTS provider]
        -> montage of 2-4 existing scene images       [VideoStage]

The recap clip is prepended to scene 0 when StoryConfig.serial_recap is
true (default: on from episode 2). Recap=off disables it. Episode 1 never
recaps.

Deterministic builder (rule-based) is the default so the feature works with
zero extra LLM calls; a prompt-based builder is a DEV2 refinement.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from storyforge.core.types import StoryConfig
from storyforge.kb.episode_summary import EpisodeSummary, EpisodeSummaryStore
from storyforge.kb.types import Fact
from storyforge.ledger.loader import UniverseLedger

_MAX_RECAP_WORDS = 90  # ~30s narration (AC2)
_RECAP_FACT_CAP = 6  # most recent facts that shaped the arc
_IMAGES_FOR_MONTAGE = 4  # 2-4 existing scene images (AC3)


class RecapPlan(BaseModel):
    """Everything the video stage needs to render a recap clip."""

    enabled: bool = False
    skip_reason: str | None = None
    script: str = ""
    word_count: int = 0
    image_paths: list[str] = Field(default_factory=list)
    subtitle_lines: list[str] = Field(default_factory=list)  # per-image captions


class RecapSegment(BaseModel):
    """A rendered recap clip, ready to prepend before scene 0 (A2 AC3).

    ``audio_path`` is the synthesized TTS clip; ``image_path`` the montage
    still used by the video stage (one still + duration=audio is enough for a
    20-30s recap).
    """

    audio_path: str
    image_path: str
    subtitle: str = ""


def should_recap(config: StoryConfig, episode_number: int) -> tuple[bool, str | None]:
    """Decision rule (AC3/AC4): off if disabled, off on episode 1, on from 2."""
    if getattr(config, "recap", True) is False:
        return False, "recap=off"
    if episode_number <= 1:
        return False, "episode 1 has no previous canon"
    return True, None


def build_recap_plan(
    config: StoryConfig,
    episode_number: int,
    ledger: UniverseLedger,
    summaries: EpisodeSummaryStore,
    universe_dir: Path,
    *,
    scenes: list[Path] | None = None,
) -> RecapPlan:
    """Deterministic recap plan from ledger facts + summaries + scene images."""
    enabled, reason = should_recap(config, episode_number)
    if not enabled:
        return RecapPlan(enabled=False, skip_reason=reason)

    facts = ledger.all_facts
    recent = sorted(facts, key=lambda f: f.episode_id)[-_RECAP_FACT_CAP:]
    if not recent:
        return RecapPlan(enabled=False, skip_reason="no established facts yet")

    lines = _script_from_facts(recent, summaries, universe_dir, config.universe)
    script = " ".join(lines).strip()
    if not script:
        return RecapPlan(enabled=False, skip_reason="empty recap script")

    return RecapPlan(
        enabled=True,
        script=script,
        word_count=len(script.split()),
        image_paths=[str(p) for p in (scenes or [])[:_IMAGES_FOR_MONTAGE]],
        subtitle_lines=_chunk_for_captions(lines),
    )


def _script_from_facts(
    recent: list[Fact], summaries: EpisodeSummaryStore, universe_dir: Path, universe_id: str
) -> list[str]:
    """One sentence per key fact, recency-capped."""
    lines: list[str] = []
    for fact in recent:
        if fact.superseded_by:
            continue  # superseded facts are not current canon
        lines.append(_fact_sentence(fact))
        if len(lines) >= 5:
            break

    # Append one sensory color line from the most recent summary (optional).
    summary = _latest_summary(summaries, universe_id)
    if summary and summary.moments:
        moment = summary.moments[-1]
        color = moment.action if moment.action else None
        if color and len(lines) < 5:
            lines.append(f"Và rồi, {color}.")

    total_words = sum(len(line.split()) for line in lines)
    while total_words > _MAX_RECAP_WORDS and len(lines) > 2:
        lines.pop()
        total_words = sum(len(line.split()) for line in lines)
    return lines


def _fact_sentence(fact: Fact) -> str:
    return fact.statement


def _latest_summary(summaries: EpisodeSummaryStore, universe_id: str) -> EpisodeSummary | None:
    # Best-effort: summaries live at <kb_data_dir>/<universe>/summaries/*.json;
    # pick the most recently modified file.
    summary_dir = summaries.root / universe_id / "summaries"
    if not summary_dir.exists():
        return None
    files = sorted(summary_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        summary = summaries.load(universe_id, path.stem)
        if summary is not None:
            return summary
    return None


def _chunk_for_captions(lines: list[str]) -> list[str]:
    """Each fact sentence becomes one subtitle line for the montage."""
    return lines


def write_recap_plan(ctx_store: object, plan: RecapPlan, out_dir: Path) -> None:
    """Persist the recap plan as an artifact for the video stage / debugging."""
    from storyforge.core.artifacts import ArtifactStore

    if isinstance(ctx_store, ArtifactStore):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "recap_plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
