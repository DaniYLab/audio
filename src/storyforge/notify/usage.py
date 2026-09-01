"""M5-V4: usage export — aggregate manifest metrics into CSV/JSONL.

Usage::

    rows = collect_usage(workspace_dir, universe_id="u1", since=datetime(...))
    export_csv(rows, path)   # "project,stage,cost_usd,tokens_in,api_calls,created_at"
    export_jsonl(rows, path)
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.exceptions import WorkspaceError
from storyforge.core.types import RunManifest, StageStatus


class UsageRow(dict[str, Any]):
    """One usage row with fields: project, stage, cost_usd, tokens_in,
    tokens_out, api_calls, created_at, universe."""


def collect_usage(
    workspace_dir: Path,
    universe: str | None = None,
    since: datetime | None = None,
) -> list[UsageRow]:
    """Scan every project in the workspace and read its manifest.

    Returns a list of UsageRow dicts, one per stage with non-zero metrics.
    """
    if not workspace_dir.exists():
        return []
    rows: list[UsageRow] = []
    for project_dir in sorted(p for p in workspace_dir.iterdir() if p.is_dir()):
        store = ArtifactStore(workspace_dir, project_dir.name)
        try:
            manifest = store.load_manifest()
        except (WorkspaceError, ValueError):
            continue

        if universe and not _manifest_matches(manifest, universe):
            continue

        for stage, record in manifest.stages.items():
            if record.status is StageStatus.PENDING:
                continue
            created = record.started_at or record.finished_at or datetime.now(tz=UTC)
            if since and created < since:
                continue

            row: UsageRow = UsageRow(
                project=manifest.project,
                stage=stage,
                cost_usd=record.metrics.get("cost_usd", 0.0) or 0.0,
                tokens_in=record.metrics.get("tokens_in", 0) or 0,
                tokens_out=record.metrics.get("tokens_out", 0) or 0,
                api_calls=record.metrics.get("api_calls", 0) or 0,
                created_at=created.isoformat(),
            )
            # Attempt to extract universe from manifest.
            if manifest.story_config is not None:
                row["universe"] = manifest.story_config.universe
            if universe:
                row["universe"] = universe
            rows.append(row)
    return rows


def _manifest_matches(manifest: RunManifest, universe: str) -> bool:
    if manifest.story_config is not None:
        return manifest.story_config.universe == universe
    return False  # no story config — can't match


def export_csv(rows: list[UsageRow], path: Path) -> None:
    """Write usage rows as CSV (header always first)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["project", "stage", "cost_usd", "tokens_in", "tokens_out",
                         "api_calls", "created_at", "universe"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def export_jsonl(rows: list[UsageRow], path: Path) -> None:
    """Write usage rows as JSONL (one JSON object per line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def since_days(days: int) -> datetime:
    """Shortcut: returns UTC datetime ``days`` ago at midnight."""
    return datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days
    )
