"""T1-DEV1: metrics recorder + cost computation against prices.yaml."""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from storyforge.core.cost import build_cost_report, classify_tier
from storyforge.core.metrics import (
    MetricsRecorder,
    bind_run_recorder,
    current_run_recorder,
    flush_into_manifest,
    reset_run_recorder,
)
from storyforge.core.types import RunManifest, StageStatus


# -- MetricsRecorder ---------------------------------------------------------


def test_record_and_snapshot() -> None:
    rec = MetricsRecorder()
    rec.record("story", llm_input_tokens=100, api_calls=1)
    rec.record("story", llm_input_tokens=50)
    snap = rec.snapshot("story")
    assert snap["llm_input_tokens"] == 150  # accumulates
    assert snap["api_calls"] == 1


def test_record_merge_vs_replace() -> None:
    rec = MetricsRecorder()
    rec.record("story", cost_usd=0.01)
    rec.record("story", cost_usd=0.02)
    rec.record("story", manual=True)
    assert rec.snapshot("story")["cost_usd"] == 0.03
    assert rec.snapshot("story")["manual"] is True  # non-numeric replaces


def test_snapshot_absent_stage() -> None:
    rec = MetricsRecorder()
    assert rec.snapshot("missing") == {}


def test_flush_resets() -> None:
    rec = MetricsRecorder()
    rec.record("story", api_calls=1)
    flushed = rec.flush()
    assert flushed == {"story": {"api_calls": 1}}
    assert rec.snapshot("story") == {}


def test_contextvar_bind_and_reset() -> None:
    assert current_run_recorder() is None
    rec = MetricsRecorder()
    bind_run_recorder(rec)
    try:
        assert current_run_recorder() is rec
    finally:
        reset_run_recorder()
    assert current_run_recorder() is None


def test_flush_into_manifest() -> None:
    rec = MetricsRecorder()
    rec.record("story", llm_input_tokens=500, cost_usd=0.001)
    manifest = RunManifest(project="p1")
    manifest.mark("story", StageStatus.DONE)
    flush_into_manifest(rec, manifest)
    assert manifest.stages["story"].metrics["llm_input_tokens"] == 500
    assert manifest.stages["story"].metrics["cost_usd"] == 0.001


# -- cost computation (prices.yaml) -----------------------------------------


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    from storyforge.core.config import Settings

    values: dict[str, object] = {
        "llm__api_key": "test-key",
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    # The dev environment may set a custom writer model; pin the default
    # (post-construction — env vars beat constructor kwargs in
    # pydantic-settings) so the cost assertions match prices.yaml.
    settings.llm.writer_model = "gpt-4o"
    settings.llm.reviewer_model = "gpt-4o-mini"
    return settings


def _settings_with_tts(tmp_path: Path, engine: str = "edge") -> Settings:
    """Settings with engine override (constructor alias doesn't work for nested)."""
    from storyforge.core.config import Settings, TTSSettings

    settings = Settings(llm__api_key="test-key", tts=TTSSettings(engine=engine))
    settings.llm.writer_model = "gpt-4o"
    settings.llm.reviewer_model = "gpt-4o-mini"
    return settings


def _manifest_with_tokens(project: str = "p1") -> RunManifest:
    manifest = RunManifest(project=project)
    manifest.mark("story", StageStatus.DONE, llm_input_tokens=1000, llm_output_tokens=500)
    manifest.mark("imaging", StageStatus.DONE, images_generated=10)
    manifest.mark("tts", StageStatus.DONE, tts_chars=2000)
    return manifest


def test_cost_report_uses_prices_yaml(tmp_path: Path) -> None:
    """AC1: cost > 0 after a run with LLM + image + tts metrics."""
    settings = _settings(tmp_path)
    manifest = _manifest_with_tokens()
    report = build_cost_report(manifest, settings)  # type: ignore[arg-type]
    assert report.total_cost_usd > 0.0
    by_stage = {row.stage: row.cost_usd for row in report.stages}
    assert by_stage["story"] > 0.0  # gpt-4o: 1000*0.0025/1000 + 500*0.01/1000
    assert by_stage["imaging"] > 0.0  # flux schnell: 10 * 0.003
    assert by_stage["tts"] == 0.0  # edge TTS is free


def test_cost_report_premium_tts(tmp_path: Path) -> None:
    """ElevenLabs TTS chars cost money."""
    settings = _settings_with_tts(tmp_path, engine="elevenlabs")
    manifest = _manifest_with_tokens()
    report = build_cost_report(manifest, settings)  # type: ignore[arg-type]
    by_stage = {row.stage: row.cost_usd for row in report.stages}
    assert by_stage["tts"] > 0.0  # 2000 chars * 0.3/1000 = 0.6


def test_classify_tier_from_prices(tmp_path: Path) -> None:
    """Tier presets come from prices.yaml, not code."""
    settings = _settings(tmp_path)
    assert classify_tier("tts", settings) == "standard"  # type: ignore[arg-type]
    settings2 = _settings_with_tts(tmp_path, engine="elevenlabs")
    assert classify_tier("tts", settings2) == "premium"  # type: ignore[arg-type]


def test_cost_report_zero_without_metrics(tmp_path: Path) -> None:
    manifest = RunManifest(project="p1")
    manifest.mark("story", StageStatus.DONE)  # no token metrics
    settings = _settings(tmp_path)
    report = build_cost_report(manifest, settings)  # type: ignore[arg-type]
    assert report.total_cost_usd == 0.0


def test_prices_yaml_exists_and_has_required_models() -> None:
    """AC2: prices.yaml has all required model defaults."""
    import yaml

    path = pathlib.Path("config/prices.yaml")
    assert path.exists(), "config/prices.yaml missing"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["llm"]["gpt-4o"]["input_per_1k"] > 0
    assert data["llm"]["gpt-4o-mini"]["input_per_1k"] > 0
    assert data["embedding"]["text-embedding-3-small"]["per_1k"] > 0
    assert data["images"]["fal-ai/flux/schnell"]["per_image"] > 0
    assert data["tts"]["edge"]["per_1k_chars"] == 0  # free
    assert data["tts"]["elevenlabs"]["per_1k_chars"] > 0
    assert "standard" in data["tiers"]
    assert "premium" in data["tiers"]


# -- T2-DEV1: _check_alert fires after 2 consecutive failures ------------------


def _alert_settings(tmp_path: Path) -> Settings:
    from storyforge.core.config import Settings

    return Settings(llm__api_key="test-key", workspace_dir=tmp_path / "workspace")


def _failed_manifest(project: str, stage: str) -> RunManifest:
    manifest = RunManifest(project=project)
    manifest.mark(stage, StageStatus.FAILED, error="boom")
    return manifest


def test_alert_fires_after_two_failures(tmp_path: Path) -> None:
    """AC2 (T2-DEV1): 2 consecutive failures of the same stage → alert fires."""
    from storyforge.cli import _check_alert

    settings = _alert_settings(tmp_path)
    project = "proj_a"
    alerts_path = tmp_path / "alerts.md"

    # First failure: no alert yet (count = 1).
    _check_alert(settings, project, _failed_manifest(project, "story"))
    assert not alerts_path.exists()

    # Second consecutive failure: alert fires (count = 2).
    _check_alert(settings, project, _failed_manifest(project, "story"))
    assert alerts_path.exists()
    text = alerts_path.read_text(encoding="utf-8")
    assert "story" in text
    assert "fail x2" in text


def test_alert_not_fire_after_single_failure(tmp_path: Path) -> None:
    from storyforge.cli import _check_alert

    settings = _alert_settings(tmp_path)
    project = "proj_b"
    _check_alert(settings, project, _failed_manifest(project, "story"))
    assert not (tmp_path / "alerts.md").exists()


def test_alert_resets_on_success(tmp_path: Path) -> None:
    """A success run resets the counter → no alert on the next failure."""
    from storyforge.cli import _check_alert

    settings = _alert_settings(tmp_path)
    project = "proj_c"
    alerts_path = tmp_path / "alerts.md"

    _check_alert(settings, project, _failed_manifest(project, "story"))
    ok = RunManifest(project=project)
    ok.mark("story", StageStatus.DONE)
    _check_alert(settings, project, ok)  # resets counter
    _check_alert(settings, project, _failed_manifest(project, "story"))
    assert not alerts_path.exists()


def test_pipeline_marks_failed_on_stage_error(tmp_path: Path) -> None:
    """AC1 (T2-DEV1): a failing stage marks FAILED in the manifest."""
    from storyforge.core.config import Settings

    settings = Settings(llm__api_key="test-key", workspace_dir=tmp_path / "workspace")
    # Simulate what _mark_failed does without invoking the whole pipeline:
    from storyforge.core.artifacts import ArtifactStore
    from storyforge.core.contracts import StageContext

    store = ArtifactStore(settings.workspace_dir, "proj_x")
    manifest = store.load_manifest()
    ctx = StageContext(settings, store, manifest)
    from storyforge.cli import _mark_failed
    from storyforge.core.exceptions import StoryForgeError

    _mark_failed(ctx, "story", StoryForgeError("boom"))
    reloaded = store.load_manifest()
    assert reloaded.stages["story"].status is StageStatus.FAILED
    assert "boom" in (reloaded.stages["story"].error or "")