"""M5-V4: usage export unit tests."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.types import RunManifest, StageStatus, StoryConfig
from storyforge.notify.usage import (
    collect_usage,
    export_csv,
    export_jsonl,
    since_days,
)


def _write_manifest(
    workspace: Path, project: str, *, universe: str = "u1", cost: float = 1.5
) -> None:
    store = ArtifactStore(workspace, project)
    manifest = RunManifest(project=project)
    manifest.mark(
        "story",
        StageStatus.DONE,
        cost_usd=cost,
        tokens_in=100,
        tokens_out=50,
        api_calls=3,
    )
    manifest.story_config = StoryConfig(
        title="T", genre="g", universe=universe
    )
    store.save_manifest(manifest)


def test_collect_usage_scans_projects(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_manifest(workspace, "p1", universe="u1", cost=2.0)
    _write_manifest(workspace, "p2", universe="u2", cost=0.5)

    rows = collect_usage(workspace)
    assert len(rows) == 2
    by_project = {r["project"]: r for r in rows}
    assert by_project["p1"]["cost_usd"] == 2.0
    assert by_project["p2"]["cost_usd"] == 0.5


def test_collect_usage_filter_by_universe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_manifest(workspace, "p1", universe="u1")
    _write_manifest(workspace, "p2", universe="u2")

    rows = collect_usage(workspace, universe="u1")
    assert len(rows) == 1
    assert rows[0]["project"] == "p1"


def test_collect_usage_empty_workspace(tmp_path: Path) -> None:
    assert collect_usage(tmp_path / "nonexistent") == []


def test_export_csv_header_and_rows(tmp_path: Path) -> None:
    rows = [
        {
            "project": "p1",
            "stage": "story",
            "cost_usd": 1.5,
            "tokens_in": 100,
            "tokens_out": 50,
            "api_calls": 3,
            "created_at": "2026-01-01T00:00:00+00:00",
            "universe": "u1",
        }
    ]
    out = tmp_path / "usage.csv"
    export_csv(rows, out)
    with out.open("r", encoding="utf-8", newline="") as fh:
        reader = list(csv.DictReader(fh))
    assert reader[0]["project"] == "p1"
    assert reader[0]["cost_usd"] == "1.5"


def test_export_jsonl(tmp_path: Path) -> None:
    rows = [
        {
            "project": "p1",
            "stage": "tts",
            "cost_usd": 0.2,
            "tokens_in": 10,
            "tokens_out": 5,
            "api_calls": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "universe": "u1",
        }
    ]
    out = tmp_path / "usage.jsonl"
    export_jsonl(rows, out)
    lines = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line
    ]
    assert lines[0]["stage"] == "tts"


def test_since_days_midnight() -> None:
    since = since_days(7)
    assert since.tzinfo is UTC
    assert since.hour == 0
    assert (datetime.now(tz=UTC) - since) > timedelta(days=6)


def test_collect_usage_skips_pending_stages(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = ArtifactStore(workspace, "p1")
    manifest = RunManifest(project="p1")
    manifest.mark("download", StageStatus.PENDING)
    manifest.mark("story", StageStatus.DONE, cost_usd=1.0)
    store.save_manifest(manifest)
    rows = collect_usage(workspace)
    assert len(rows) == 1
    assert rows[0]["stage"] == "story"
