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
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_timeout_seconds: float = 30.0
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    chunk_size_tokens: int = 600
    chunk_overlap_tokens: int = 90
    retrieval_top_k: int = 8


class TTSSettings(BaseModel):
    engine: Literal["edge", "elevenlabs"] = "edge"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    edge_voice: str = "vi-VN-NamMinhNeural"
    elevenlabs_api_key: SecretStr = SecretStr("")
    elevenlabs_voice_id: str = ""


class ImagingSettings(BaseModel):
    provider: Literal["fal", "openai"] = "fal"
    fal_key: SecretStr = SecretStr("")
    fal_model: str = "fal-ai/flux/schnell"
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
    tts: TTSSettings = Field(default_factory=TTSSettings)
    imaging: ImagingSettings = Field(default_factory=ImagingSettings)
    video: VideoSettings = Field(default_factory=VideoSettings)

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
