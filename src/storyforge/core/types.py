"""Domain models shared across stages.

Rules: these are pure data structures (pydantic models) with no I/O and no
provider-specific fields. Every artifact written to the workspace is one of
these models serialized as JSON, which keeps stage boundaries stable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

SourceId = str  # e.g. YouTube video id or local-file slug

License = Literal["cc0", "cc_by", "owned", "permission", "unknown"]  # M3 §8.3


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class SourceRef(BaseModel):
    """A single ingested audio source."""

    id: SourceId
    url: str | None = None
    local_path: Path | None = None
    title: str | None = None
    channel: str | None = None
    duration_seconds: float | None = None
    license: License = "unknown"  # M3 §8.3: ingest --license, default unknown
    ingested_at: datetime = Field(default_factory=utc_now)


class TranscriptSegment(BaseModel):
    start: float  # seconds from audio start
    end: float
    text: str
    speaker: str | None = None


class Transcript(BaseModel):
    source: SourceRef
    language: str
    segments: list[TranscriptSegment]
    audio_path: Path

    @property
    def full_text(self) -> str:
        return " ".join(seg.text.strip() for seg in self.segments).strip()


class KnowledgeChunk(BaseModel):
    chunk_id: str
    text: str
    token_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class CharacterSheet(BaseModel):
    """Character bible entry — drives both prose consistency and image prompts."""

    name: str
    appearance: str  # fixed visual description reused verbatim in image prompts
    personality: str
    speech_style: str | None = None
    tts_voice: str | None = None  # per-character voice override


class StoryStyle(BaseModel):
    tone: str = "warm, contemplative"
    pov: Literal["first_person", "third_limited", "omniscient"] = "third_limited"
    tense: Literal["past", "present"] = "past"
    art_style: str = "watercolor illustration, soft palette, cinematic lighting"


class GroundingLevel(StrEnum):
    STRICT = "strict"  # only events/characters present in the knowledge base
    LOOSE = "loose"  # take inspiration; invent freely around retrieved facts


class StoryConfig(BaseModel):
    """User-facing story specification (config/story_config.yaml)."""

    title: str
    genre: str
    universe: str  # long-lived story canon; REQUIRED — no silent default (KB design §2.1)
    season: str | None = None  # episode group within a universe; None means s0
    target_minutes: float = Field(default=5.0, gt=0)
    language: str = "vi"
    style: StoryStyle = StoryStyle()
    characters: list[CharacterSheet] = Field(default_factory=list)
    grounding: GroundingLevel = GroundingLevel.LOOSE
    premise: str = ""  # one-paragraph seed idea; may be empty for pure-KB stories
    source_query: str | None = None  # KB retrieval query; None = use premise only
    # M3-W5: mood tag for the CC0 music bed (e.g. "warm", "tense", "sad").
    # None = no music bed mixed into the final video.
    music_mood: str | None = None

    @field_validator("universe")
    @classmethod
    def _universe_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(
                "universe is required — set it explicitly (no default). "
                "See KNOWLEDGE_BASE_DESIGN.md §2.1."
            )
        return value


class StoryBeat(BaseModel):
    """One outline beat; a scene is a beat expanded by the writer LLM."""

    beat_id: str
    summary: str
    characters: list[str] = Field(default_factory=list)
    image_hint: str = ""  # visual scene description used by the imaging stage
    # M3 §0 contract freeze: declared plot twist. When "twist", the reviewer
    # treats contradictions within this beat as intentional character
    # development (TWIST_OK), not hallucination.
    intent: Literal["normal", "twist"] = "normal"


class StoryScene(BaseModel):
    scene_id: str
    beat: StoryBeat
    narration_text: str  # what the TTS reads
    image_prompt: str  # full prompt for the imaging stage (includes characters' appearance)
    facts_used: list[str] = Field(default_factory=list)


class Story(BaseModel):
    config: StoryConfig
    outline: list[StoryBeat]
    scenes: list[StoryScene]
    created_at: datetime = Field(default_factory=utc_now)


class NarrationClip(BaseModel):
    """TTS output for one scene."""

    scene_id: str
    audio_path: Path
    duration_seconds: float
    char_count: int
    # M2-D1 §1.1: the normalized text actually sent to TTS (subtitles keep the
    # original narration). Used for audit/debug of normalization behavior.
    normalized_text: str | None = None


class Illustration(BaseModel):
    """Generated image for one scene."""

    scene_id: str
    image_path: Path
    prompt_hash: str  # provenance: which prompt produced this image


class SubtitleLine(BaseModel):
    index: int
    start: float
    end: float
    text: str


class VideoResult(BaseModel):
    video_path: Path
    duration_seconds: float
    scene_count: int
    ffmpeg_command_log: Path


# --- M2-D4: prompt-eval harness ----------------------------------------------


class DimensionScore(BaseModel):
    """One rubric dimension score from the judge LLM (1.0-5.0, 0.5 steps)."""

    dimension: Literal["grounding", "consistency", "pacing", "tts_ready", "visual", "hook"]
    score: float  # 1.0-5.0, step 0.5
    evidence: str  # quoted passage as evidence


class StoryEval(BaseModel):
    """Judge verdict for one scene (or the whole episode when scene_id="episode")."""

    prompt_version: int
    project: str
    scene_id: str  # "episode" when scoring the whole story
    scores: list[DimensionScore]
    total: float  # mean of 6 dimensions * 20 -> 0..100 scale
    judge_model: str
    created_at: datetime = Field(default_factory=utc_now)


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # upstream artifact already present (resume)


class StageRecord(BaseModel):
    stage: str
    status: StageStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)  # e.g. cost, durations


class RunManifest(BaseModel):
    """Per-project manifest tracking every stage run — the resume backbone."""

    project: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    stages: dict[str, StageRecord] = Field(default_factory=dict)
    story_config: StoryConfig | None = None

    def mark(self, stage: str, status: StageStatus, **extra: Any) -> None:
        record = self.stages.get(stage) or StageRecord(stage=stage, status=status)
        record.status = status
        if status is StageStatus.RUNNING:
            record.started_at = utc_now()
        if status in (StageStatus.DONE, StageStatus.FAILED, StageStatus.SKIPPED):
            record.finished_at = utc_now()
        record.metrics.update(extra)
        self.stages[stage] = record
        self.updated_at = utc_now()
