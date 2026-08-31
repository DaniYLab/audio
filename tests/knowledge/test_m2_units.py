"""M2-V3/V4 unit tests — alias review operations + cost report aggregation."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.core.config import (
    ImagingSettings,
    Settings,
    TranscriptionSettings,
    TTSSettings,
)
from storyforge.core.cost import build_cost_report, classify_tier
from storyforge.core.types import RunManifest, StageRecord, StageStatus
from storyforge.kb.alias import AliasStore


@pytest.fixture()
def alias_path(tmp_path: Path) -> Path:
    return tmp_path / "kb" / "demo" / "aliases.yaml"


@pytest.fixture()
def alias(alias_path: Path) -> AliasStore:
    store = AliasStore(alias_path)
    store.add_pending("Bà Ngoại", "person")
    store.add_pending("Bà Ngoại Sr.", "person")  # second pending entry
    store.save()
    return AliasStore(alias_path)  # reload from disk


# --- alias review (M2-V3) -------------------------------------------------------


def test_confirm_promotes_pending(alias: AliasStore):
    assert alias.confirm("bà ngoại") is True  # case-insensitive match
    entry = alias.entry_for("Bà Ngoại")
    assert entry is not None and entry.status == "confirmed"


def test_confirm_idempotent(alias: AliasStore):
    alias.confirm("Bà Ngoại")
    assert alias.confirm("Bà Ngoại") is False


def test_merge_alias_folds_variant_and_drops_standalone(alias: AliasStore):
    # "Bà Ngoại Sr." is a false split of "Bà Ngoại".
    assert alias.merge_alias("Bà Ngoại Sr.", "Bà Ngoại") is True
    target = alias.entry_for("Bà Ngoại")
    assert target is not None and "Bà Ngoại Sr." in target.aliases
    # The standalone entry is gone; the variant now resolves to the target.
    assert all(e.canonical != "Bà Ngoại Sr." for e in alias.all_entries())
    assert alias.resolve("bà ngoại sr.") == "Bà Ngoại"


def test_merge_alias_unknown_target(alias: AliasStore):
    assert alias.merge_alias("ai đó", "Không Tồn Tại") is False


def test_merge_alias_noop_when_variant_present(alias: AliasStore):
    assert alias.merge_alias("Bà Ngoại Sr.", "Bà Ngoại") is True
    assert alias.merge_alias("bà ngoại sr.", "Bà Ngoại") is False  # already there


def test_remove_drops_entry(alias: AliasStore):
    assert alias.remove("Bà Ngoại Sr.") is True
    assert alias.entry_for("Bà Ngoại Sr.") is None
    assert alias.remove("Bà Ngoại Sr.") is False


def test_audit_log_append_and_read(alias: AliasStore, alias_path: Path):
    alias.confirm("Bà Ngoại")
    alias.audit(actor="human", action="confirm", name="Bà Ngoại")
    alias.audit(actor="human", action="merge", name="Bà Ngoại Sr.", into="Bà Ngoại")
    rows = alias.read_audit()
    assert [r["action"] for r in rows] == ["confirm", "merge"]
    assert rows[1]["into"] == "Bà Ngoại"
    # The log is a sibling of aliases.yaml, append-only.
    assert (alias_path.parent / "alias_audit.log").exists()


# --- cost report (M2-V4) ----------------------------------------------------------


def _settings(tts: str = "edge", imaging: str = "fal", stt: str = "whisperx") -> Settings:
    settings = Settings(llm__api_key="test-key")
    settings.tts = TTSSettings(engine=tts)  # type: ignore[arg-type]
    settings.imaging = ImagingSettings(
        provider="fal" if imaging != "openai" else "openai",  # type: ignore[arg-type]
        fal_model="fal-ai/flux/schnell" if imaging == "schnell" else "fal-ai/flux/pro",
    )
    settings.transcription = TranscriptionSettings(engine=stt)  # type: ignore[arg-type]
    return settings


def test_classify_tier_from_engine_choices():
    assert classify_tier("tts", _settings(tts="edge")) == "standard"
    assert classify_tier("tts", _settings(tts="elevenlabs")) == "premium"
    assert classify_tier("imaging", _settings(imaging="schnell")) == "standard"
    assert classify_tier("imaging", _settings(imaging="fal")) == "premium"
    assert classify_tier("imaging", _settings(imaging="openai")) == "premium"
    assert classify_tier("transcribe", _settings(stt="whisperx")) == "standard"
    assert classify_tier("transcribe", _settings(stt="assemblyai")) == "premium"
    assert classify_tier("story", _settings()) == "standard"
    assert classify_tier("video", _settings()) == "standard"


def test_build_cost_report_aggregates_usd_metrics(tmp_path: Path):
    manifest = RunManifest(project="demo")
    manifest.stages["story"] = StageRecord(
        stage="story",
        status=StageStatus.DONE,
        metrics={"scenes": 8, "llm_cost_usd": 0.42, "requests": 10},
    )
    manifest.stages["tts"] = StageRecord(
        stage="tts", status=StageStatus.DONE, metrics={"clips": 8, "tts_cost_usd": 0.0}
    )
    report = build_cost_report(manifest, _settings())

    assert report.project == "demo"
    assert report.total_cost_usd == pytest.approx(0.42)
    assert report.by_tier["standard"] == pytest.approx(0.42)
    story_row = next(row for row in report.stages if row.stage == "story")
    assert story_row.cost_usd == pytest.approx(0.42)
    assert story_row.metrics["scenes"] == 8  # non-usd metrics ride along


def test_build_cost_report_splits_tiers(tmp_path: Path):
    manifest = RunManifest(project="demo")
    manifest.stages["story"] = StageRecord(
        stage="story", status=StageStatus.DONE, metrics={"llm_cost_usd": 0.3}
    )
    report = build_cost_report(manifest, _settings(tts="elevenlabs"))
    manifest.stages["tts"] = StageRecord(
        stage="tts", status=StageStatus.DONE, metrics={"tts_cost_usd": 0.5}
    )
    report = build_cost_report(manifest, _settings(tts="elevenlabs"))
    assert report.by_tier == {"standard": pytest.approx(0.3), "premium": pytest.approx(0.5)}
    assert report.total_cost_usd == pytest.approx(0.8)


def test_build_cost_report_empty_manifest():
    report = build_cost_report(RunManifest(project="x"), _settings())
    assert report.total_cost_usd == 0.0
    assert report.stages == []
