"""Per-stage cost report — aggregates manifest metrics (M2-V4, ROADMAP US3).

Data source: the run manifest. Stages already record metrics via
``StageContext.mark_done(**metrics)``; any metric key ending in ``_usd`` is a
cost and gets summed. Until providers emit cost metrics the report shows the
metric inventory and $0.00 — the aggregation backend is the deliverable.

Tier classification (ROADMAP §1): Standard = Edge-TTS + Flux Schnell +
local WhisperX; Premium = ElevenLabs, hosted/pro image models, AssemblyAI.
The mapping is derived from the run's settings (engine choices), not from
the stage name alone, so a Standard-tier run never reports Premium rows.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from storyforge.core.config import Settings
from storyforge.core.types import RunManifest, StageStatus, utc_now

_STANDARD = "standard"
_PREMIUM = "premium"


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


def classify_tier(stage: str, settings: Settings) -> str:
    """Map a pipeline stage to its cost tier from the run's engine choices."""
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
    # story/llm, knowledge, download, video: local or flat-rate stages.
    return _STANDARD


def build_cost_report(manifest: RunManifest, settings: Settings) -> CostReport:
    rows: list[StageCostRow] = []
    for stage in sorted(manifest.stages):
        record = manifest.stages[stage]
        cost = sum(
            float(value)
            for key, value in record.metrics.items()
            if key.endswith("_usd") and isinstance(value, int | float)
        )
        rows.append(
            StageCostRow(
                stage=stage,
                tier=classify_tier(stage, settings),
                cost_usd=round(cost, 6),
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
    """Stages that actually ran (for the report's completion context)."""
    return [
        stage
        for stage, record in manifest.stages.items()
        if record.status in (StageStatus.DONE, StageStatus.SKIPPED)
    ]


__all__ = ["CostReport", "StageCostRow", "build_cost_report", "classify_tier", "completed_stages"]
