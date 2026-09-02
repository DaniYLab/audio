"""Per-stage cost report — aggregates manifest metrics against prices.yaml.

Reads ``config/prices.yaml`` to compute dollar amounts from raw metrics
(tokens, images, chars, seconds). Supports ``--tier standard|premium``
filtering (T1-DEV1).

Tier classification (m2_design §5.4): defined in prices.yaml — a preset of
(tts engine + image model + stt engine), not code logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from storyforge.core.config import Settings
from storyforge.core.types import RunManifest, StageStatus, utc_now

_STANDARD = "standard"
_PREMIUM = "premium"

_PRICES_PATH = Path("config/prices.yaml")


class StageCostRow(BaseModel):
    stage: str
    tier: str
    cost_usd: float = 0.0
    metrics: dict[str, Any] = Field(default_factory=dict)


class CostReport(BaseModel):
    project: str
    generated_at: str
    total_cost_usd: float = 0.0
    by_tier: dict[str, float] = Field(default_factory=dict)
    stages: list[StageCostRow] = Field(default_factory=list)


def _load_prices() -> dict[str, Any]:
    """Load prices.yaml (returns empty dict on missing file)."""
    if not _PRICES_PATH.exists():
        return {}
    import yaml

    return yaml.safe_load(_PRICES_PATH.read_text(encoding="utf-8")) or {}


def classify_tier(stage: str, settings: Settings) -> str:
    """Map a pipeline stage to its cost tier from the run's engine choices.

    Tier presets live in ``config/prices.yaml`` (``tiers:``): each tier
    names its tts engine, image models and stt engine. Falls back to the
    historical code logic when the file is missing.
    """
    prices = _load_prices()
    tiers = prices.get("tiers", {})
    if isinstance(tiers, dict) and tiers:
        for tier_name, preset in tiers.items():
            if not isinstance(preset, dict):
                continue
            if stage == "tts" and settings.tts.engine == preset.get("tts"):
                return str(tier_name)
            if stage == "transcribe" and settings.transcription.engine == preset.get("stt"):
                return str(tier_name)
            if stage == "imaging":
                model = (
                    settings.imaging.fal_model
                    if settings.imaging.provider == "fal"
                    else "gpt-image-1"
                )
                image_models = preset.get("images")
                if isinstance(image_models, list) and model in image_models:
                    return str(tier_name)
        # Engine matches no preset — fall through to legacy logic below.
    if stage == "tts":
        return _PREMIUM if settings.tts.engine == "elevenlabs" else _STANDARD
    if stage == "imaging":
        provider = settings.imaging.provider
        model = settings.imaging.fal_model
        if provider == "openai":
            return _PREMIUM
        return _STANDARD if "schnell" in model.lower() else _PREMIUM
    if stage == "transcribe":
        return _PREMIUM if settings.transcription.engine == "assemblyai" else _STANDARD
    return _STANDARD


def _compute_cost(
    metrics: dict[str, Any], stage: str, prices: dict[str, Any], settings: Settings
) -> float:
    """Compute cost from raw metrics using prices.yaml (T1-DEV1).

    Falls back to aggregated ``*_usd`` metrics when prices.yaml is absent.
    """
    # If prices.yaml has the data, compute from tokens/images/chars.
    if prices:
        cost = _compute_from_prices(metrics, stage, prices, settings)
        if cost is not None:
            return round(cost, 6)

    # Fallback: sum *_usd keys.
    return sum(
        float(value)
        for key, value in metrics.items()
        if key.endswith("_usd") and isinstance(value, int | float)
    )


def _compute_from_prices(
    metrics: dict[str, Any], stage: str, prices: dict[str, Any], settings: Settings
) -> float | None:
    total = 0.0
    had_metric = False

    # LLM tokens
    llm_model = _model_for_stage(stage, settings)
    if llm_model:
        input_tokens = int(metrics.get("llm_input_tokens", 0) or 0)
        output_tokens = int(metrics.get("llm_output_tokens", 0) or 0)
        if input_tokens or output_tokens:
            had_metric = True
            llm_prices = prices.get("llm", {})
            model_prices = llm_prices.get(llm_model, {})
            total += input_tokens * float(model_prices.get("input_per_1k", 0) or 0) / 1000
            total += output_tokens * float(model_prices.get("output_per_1k", 0) or 0) / 1000

    # Images
    if stage == "imaging":
        images = int(metrics.get("images_generated", 0) or 0)
        if images:
            had_metric = True
            img_prices = prices.get("images", {})
            image_model = settings.imaging.fal_model if settings.imaging.provider == "fal" else "gpt-image-1"
            per_image = float(img_prices.get(image_model, {}).get("per_image", 0) or 0)
            total += images * per_image

    # TTS chars
    if stage == "tts":
        chars = int(metrics.get("tts_chars", 0) or 0)
        if chars:
            had_metric = True
            tts_prices = prices.get("tts", {})
            engine = settings.tts.engine
            per_1k = float(tts_prices.get(engine, {}).get("per_1k_chars", 0) or 0)
            total += chars * per_1k / 1000

    return total if had_metric else None


def _model_for_stage(stage: str, settings: Settings) -> str | None:
    """Return the LLM model name used by a stage, or None."""
    if stage in ("story", "review", "knowledge"):
        if stage == "review":
            return settings.llm.reviewer_model
        return settings.llm.writer_model
    return None


def build_cost_report(manifest: RunManifest, settings: Settings) -> CostReport:
    prices = _load_prices()
    rows: list[StageCostRow] = []
    for stage in sorted(manifest.stages):
        record = manifest.stages[stage]
        cost = _compute_cost(record.metrics, stage, prices, settings)
        rows.append(
            StageCostRow(
                stage=stage,
                tier=classify_tier(stage, settings),
                cost_usd=cost,
                metrics=dict(record.metrics),
            )
        )

    by_tier: dict[str, float] = {}
    for row in rows:
        by_tier[row.tier] = round(by_tier.get(row.tier, 0.0) + row.cost_usd, 6)

    return CostReport(
        project=manifest.project,
        generated_at=utc_now().isoformat(),
        total_cost_usd=round(sum(by_tier.values()), 6),
        by_tier=by_tier,
        stages=rows,
    )


def completed_stages(manifest: RunManifest) -> list[str]:
    return [
        stage
        for stage, record in manifest.stages.items()
        if record.status in (StageStatus.DONE, StageStatus.SKIPPED)
    ]


__all__ = [
    "CostReport",
    "StageCostRow",
    "build_cost_report",
    "classify_tier",
    "completed_stages",
]
