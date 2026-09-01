"""Typed application settings.

Precedence (highest wins): environment variables (SF__SECTION__KEY) > .env file
> config/settings.yaml > code defaults. Secrets must NEVER live in code or in
settings.yaml — only in env/.env, which is gitignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from storyforge.core.exceptions import ConfigError


class DownloadSettings(BaseModel):
    acknowledge_tos_risk: bool = False
    audio_format: Literal["m4a", "mp3", "opus"] = "m4a"
    audio_quality: int = 0  # VBR best
    rate_limit: str = "5/s"  # be a polite client


class TranscriptionSettings(BaseModel):
    engine: Literal["whisperx", "assemblyai"] = "whisperx"
    model: str = "large-v3"
    language: str = "vi"
    device: Literal["cuda", "cpu"] = "cpu"
    compute_type: Literal["float16", "int8"] = "int8"
    batch_size: int = 16
    assemblyai_api_key: SecretStr = SecretStr("")


class LLMSettings(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    writer_model: str = "gpt-4o"
    reviewer_model: str = "gpt-4o-mini"
    timeout_seconds: float = 120.0
    max_output_tokens: int = 8192

    # NOTE: the api_key is deliberately NOT validated here. Settings loads for
    # every command (kb-health, status, ...); commands that never call the LLM
    # must not be blocked by a missing key. Fail-fast happens in LLMClient,
    # where the key is actually used.


class EmbeddingSettings(BaseModel):
    provider: Literal["openai", "bge_m3_local"] = "openai"
    model: str = "text-embedding-3-small"
    base_url: str = ""  # empty = reuse LLM base_url
    api_key: SecretStr = SecretStr("")  # empty = reuse LLM api_key
    dimensions: int = 1536


class KnowledgeSettings(BaseModel):
    store: Literal["chroma", "qdrant", "memory"] = "qdrant"
    collection: str = "storyforge_kb"
    collection_prefix: str = "storyforge_kb"  # Qdrant: <prefix>_<universe>
    persist_dir: Path = Path("data/kb")
    kb_data_dir: Path = Path("data/kb")  # alias tables: <dir>/<universe>/aliases.yaml
    ledgers_dir: Path = Path("data/ledgers")  # fact ledgers: <dir>/<universe>/s<N>/ep<M>.yaml
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_timeout_seconds: float = 30.0
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    chunk_size_tokens: int = 600
    chunk_overlap_tokens: int = 90
    retrieval_top_k: int = 8
    # M2 §5.1: reranker stays a measured flag until ARCH signs the default
    # (J2 hit-rate +>= 3pp AND p95 < 2s). Env: SF__KNOWLEDGE__USE_RERANKER.
    use_reranker: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # M2 §5.2: 1 LLM call per fresh source at ingest (default ON in M2).
    # Env: SF__KNOWLEDGE__EPISODE_SUMMARY.
    episode_summary: bool = True
    # M3 §8.3: publish license gate; empty = allow all. Unknown sources are
    # always flagged with license_warning=True in the IngestReport.
    allowed_licenses: list[str] = Field(default_factory=list)


class LedgerSettings(BaseModel):
    """Fact-ledger behaviour (M3 §1, M4-B4 LLM arbiter)."""

    # M4-B4: escalate to an LLM arbiter when rule-based conflict detection is
    # uncertain (same slot, different statement, ambiguous negation). Off by
    # default until a rule-based baseline exists (AC4). Env:
    # SF__LEDGER__ARBITER_ENABLED.
    arbiter_enabled: bool = False
    # Which model arbitrates (default: SF__LLM__WRITER_MODEL).
    arbiter_model: str | None = None


class TTSSettings(BaseModel):
    engine: Literal["edge", "elevenlabs"] = "edge"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    edge_voice: str = "vi-VN-NamMinhNeural"
    normalize_text: bool = True  # M2-W1: expand digits/abbreviations before TTS
    cache_dir: Path = Path("data/cache/tts")  # M3-W3: audio cache by hash
    elevenlabs_api_key: SecretStr = SecretStr("")
    elevenlabs_voice_id: str = ""


class ImagingSettings(BaseModel):
    provider: Literal["fal", "openai"] = "fal"
    fal_key: SecretStr = SecretStr("")
    fal_model: str = "fal-ai/flux/schnell"
    fal_ref_model: str = "fal-ai/flux-pro/kontext"  # M3-W4: reference-image model
    openai_model: str = "gpt-image-1"
    width: int = 1920
    height: int = 1080


class VideoSettings(BaseModel):
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "medium"
    ken_burns: bool = True
    burn_subtitles: bool = True
    transition_seconds: float = 0.5
    # M3 §6: hardware-accelerated encode. "auto" probes ffmpeg encoders once
    # and prefers nvenc > qsv > libx264 (result cached in the workspace).
    encoder: Literal["auto", "libx264", "h264_nvenc", "h264_qsv"] = "auto"


class AnimationSettings(BaseModel):
    """M6-W1: image-to-video animation provider."""

    provider: Literal["auto", "kenburns", "fal_kling", "veo"] = "auto"
    motion_default: Literal["kenburns", "slow_push", "pan", "subtle_zoom"] = "slow_push"
    min_duration_seconds: float = 3.0
    budget_per_video_usd: float = 2.0


class StorySettings(BaseModel):
    """M4-B1: deterministic style statistics + M4 A1/A3 knobs."""

    style_stats: bool = True  # SF__STORY__STYLE_STATS
    hook: Literal["auto", "a", "b", "manual"] = "auto"  # M4-A1 AC3
    thumbnail_scene: str = "auto"  # M4-A3 AC3: "auto" | scene id/index
    # M6-W3: ctxpack — compact the brief when estimated tokens exceed this
    # (0 = off). Keeps serial episodes (20+) within the LLM context budget.
    compact_when_over_tokens: int = 0
    compact_keep_recent_episodes: int = 10


class APISettings(BaseModel):
    """M5-W1: REST API v1 — JWT auth, rate limiting."""

    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 60
    rate_limit_per_minute: int = 120
    users_file: Path = Path("data/secrets/users.json")  # dev users registry


class QueueSettings(BaseModel):
    """M4-A6: shared job queue with lease TTL + heartbeat (multi-worker)."""

    queue_dir: Path = Path("data/queue")
    lease_ttl_seconds: int = 30
    heartbeat_interval_seconds: int = 10
    worker_id: str = ""  # default: hostname
    scheduled_grace_seconds: int = 0  # jobs not yet due are skipped


class PublishSettings(BaseModel):
    """M4-A4: YouTube upload (draft mode) — credentials via env only."""

    youtube_client_id: str = ""
    youtube_client_secret: SecretStr = SecretStr("")
    youtube_refresh_token: SecretStr = SecretStr("")
    metadata_template: str = "%{title}"
    upload_enabled: bool = False  # SF__PUBLISH__UPLOAD_ENABLED
    token_path: Path = Path("data/secrets/youtube_token.json")


class AnalyticsSettings(BaseModel):
    """M7-V3: analytics ingestion — YouTube + local warehouse knobs."""

    warehouse_dir: Path = Path("data/analytics")
    retention_cache_ttl_hours: float = 24.0  # SF__ANALYTICS__RETENTION_CACHE_TTL_HOURS
    # M7-W3: agentic proposal loop (default OFF until there is real data).
    agentic_enabled: bool = False  # SF__ANALYTICS__AGENTIC_ENABLED
    low_retention_threshold: float = 0.4  # SF__ANALYTICS__LOW_RETENTION_THRESHOLD


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Vars look like SF__SECTION__KEY (see .env.example): prefix "SF__"
        # plus the "__"-joined nested field path.
        env_prefix="SF__",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    workspace_dir: Path = Path("data/workspace")
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # default_factory (not eager instances) so importing this module never
    # validates sub-settings before env/.env values are applied.
    download: DownloadSettings = Field(default_factory=DownloadSettings)
    transcription: TranscriptionSettings = Field(default_factory=TranscriptionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    ledger: LedgerSettings = Field(default_factory=LedgerSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    imaging: ImagingSettings = Field(default_factory=ImagingSettings)
    video: VideoSettings = Field(default_factory=VideoSettings)
    animation: AnimationSettings = Field(default_factory=AnimationSettings)
    story: StorySettings = Field(default_factory=StorySettings)
    api: APISettings = Field(default_factory=APISettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)
    analytics: AnalyticsSettings = Field(default_factory=AnalyticsSettings)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Settings:
        """Load settings from an optional YAML file, then apply env overrides."""
        overrides: dict[str, object] = {}
        if config_path is not None and config_path.exists():
            with config_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if not isinstance(loaded, dict):
                raise ConfigError(f"{config_path} must contain a YAML mapping")
            overrides = loaded
        try:
            return cls(**overrides)  # type: ignore[arg-type]
        except Exception as exc:  # pydantic ValidationError and friends
            raise ConfigError(f"invalid settings: {exc}") from exc
