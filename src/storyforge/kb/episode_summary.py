"""EpisodeSummary — 1 cheap LLM call per source at ingest (design v4 §3).

Why: J1 (premise pack) retrieval improves when the store knows each source's
"notable moments" instead of only raw chunks. The summary is NOT embedded —
it is stored as JSON next to the alias table and joined into briefs by
source id.

Failure policy: best-effort. A failed summary must never fail the ingest
(transcript is DONE regardless); the warning is logged and the summary file
simply absent — the next non-noop ingest of that source regenerates it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from storyforge.core.config import Settings
from storyforge.core.types import Transcript, utc_now


class SummaryMoment(BaseModel):
    """One concrete storytelling beat — person, place, action, sensory detail."""

    person: str = "không rõ"
    place: str = "không rõ"
    action: str
    detail: str = "không rõ"


class EpisodeSummary(BaseModel):
    source_id: str
    universe_id: str
    moments: list[SummaryMoment] = Field(default_factory=list)
    model: str = ""
    generated_at: float = Field(default_factory=lambda: utc_now().timestamp())


@runtime_checkable
class EpisodeSummarizer(Protocol):
    def summarize(self, transcript: Transcript) -> EpisodeSummary: ...


class LLMEpisodeSummarizer:
    """Real summarizer: writer model + prompts/episode_summary.txt.

    The LLM client is built lazily so constructing the summarizer never
    requires an API key (stores build it only when the ingest flag is on).
    """

    def __init__(self, settings: Settings, universe_id: str) -> None:
        self._settings = settings
        self._universe_id = universe_id

    def summarize(self, transcript: Transcript) -> EpisodeSummary:
        from storyforge.providers.llm import LLMClient, fill_prompt, load_prompt

        # M2 §5.2: writer model (the summarizer shapes the writer's J1 input).
        client = LLMClient(self._settings, self._settings.llm.writer_model)
        template = load_prompt("episode_summary")
        response = client.chat(
            system=fill_prompt(template, {"language": transcript.language}),
            user=fill_prompt(template, {"transcript": transcript.full_text[:12000]}),
        )
        return EpisodeSummary(
            source_id=transcript.source.id,
            universe_id=self._universe_id,
            moments=parse_moments(response),
            model=self._settings.llm.writer_model,
        )


class EpisodeSummaryStore:
    """JSON file per source under ``<kb_data_dir>/<universe>/summaries/``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, universe_id: str, source_id: str) -> Path:
        return self.root / universe_id / "summaries" / f"{source_id}.json"

    def save(self, summary: EpisodeSummary) -> Path:
        path = self.path_for(summary.universe_id, summary.source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, universe_id: str, source_id: str) -> EpisodeSummary | None:
        path = self.path_for(universe_id, source_id)
        if not path.exists():
            return None
        try:
            return EpisodeSummary.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            return None  # corrupted summary: treat as absent, regenerate later


def parse_moments(response: str) -> list[SummaryMoment]:
    """Parse 'MOMENT: person | place | action | detail' lines."""
    moments: list[SummaryMoment] = []
    for line in response.splitlines():
        line = line.strip()
        if not line.upper().startswith("MOMENT:"):
            continue
        parts = [p.strip() for p in line[len("MOMENT:") :].split("|")]
        if len(parts) < 3 or not parts[2]:
            continue
        moments.append(
            SummaryMoment(
                person=parts[0] or "không rõ",
                place=parts[1] or "không rõ",
                action=parts[2],
                detail=parts[3] if len(parts) > 3 and parts[3] else "không rõ",
            )
        )
    return moments


def summary_text(summary: EpisodeSummary) -> str:
    """Human-readable one-line-per-moment text for the chunk payload."""
    return "\n".join(f"{m.person} | {m.place} | {m.action} | {m.detail}" for m in summary.moments)
