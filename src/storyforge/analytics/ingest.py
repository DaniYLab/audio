"""M7-V3: analytics ingestion — pull YouTube stats + retention into a local
warehouse (file-based until Postgres M5 lands).

The ingest pipeline mirrors the publish receipts: every video that was
published (has a receipt in ``07_video/publish.json``) gets its stats and
retention pulled once per cache TTL.

Layout::

    data/analytics/
    ├── <video_id>.json              # VideoStats
    └── retention/<video_id>.json    # RetentionCurve
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from storyforge.core.exceptions import StoryForgeError
from storyforge.core.logging import get_logger
from storyforge.core.types import NarrationClip
from storyforge.publish.receipt import load_receipt

logger = get_logger(__name__)

_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_RETENTION_URL = "https://www.googleapis.com/youtubeAnalytics/v2/reports"


class AnalyticsError(StoryForgeError):
    """Analytics pull failed (network, auth, missing video)."""


class VideoStats(BaseModel):
    """One row of per-video analytics (M7-V3 §2.3 ``video_stats``)."""

    video_id: str
    project: str = ""
    channel: str = ""
    views: int = 0
    watch_time_min: float = 0.0
    avg_view_pct: float = 0.0  # 0..100
    published_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    pulled_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class RetentionCurve(BaseModel):
    """Per-second retention: (segment_idx, view_pct 0..1)."""

    video_id: str
    segments: list[tuple[int, float]] = Field(default_factory=list)  # (idx%, pct)
    pulled_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class SceneRetention(BaseModel):
    """Retention mapped onto one scene (M7-V3 ``scene_retention`` view)."""

    video_id: str
    scene_id: str
    avg_view_pct: float = 0.0  # 0..1


class AnalyticsIngestor:
    """Pull per-video stats and retention, upserting into the warehouse."""

    def __init__(
        self,
        warehouse_dir: Path,
        access_token: str = "",
        http: httpx.Client | None = None,
        cache_ttl_hours: float = 24.0,
    ) -> None:
        self.warehouse_dir = warehouse_dir
        self.retention_dir = warehouse_dir / "retention"
        self.warehouse_dir.mkdir(parents=True, exist_ok=True)
        self.retention_dir.mkdir(parents=True, exist_ok=True)
        self.access_token = access_token
        self.cache_ttl_hours = cache_ttl_hours
        self._http = http

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        return self._http

    # -- pull ---------------------------------------------------------------

    def pull_video_stats(self, video_id: str, project: str = "", channel: str = "") -> VideoStats:
        """Fetch statistics for one video via the Data API v3."""
        resp = self.http.get(
            _VIDEOS_URL,
            params={"part": "statistics,snippet", "id": video_id},
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        if resp.status_code != 200:
            raise AnalyticsError(
                "video stats pull failed",
                details={"status": resp.status_code, "body": resp.text[:300]},
            )
        items = resp.json().get("items", [])
        if not items:
            raise AnalyticsError(f"video not found: {video_id}")
        item = items[0]
        stats = item.get("statistics", {})
        snippet = item.get("snippet", {})
        views = int(stats.get("viewCount", 0) or 0)
        avg_view_pct = float(stats.get("averageViewPercentage", 0.0) or 0.0)
        watch_min = round(views * avg_view_pct / 100.0, 2)
        return VideoStats(
            video_id=video_id,
            project=project,
            channel=channel or str(snippet.get("channelTitle", "")),
            views=views,
            watch_time_min=watch_min,
            avg_view_pct=avg_view_pct,
        )

    def pull_retention(self, video_id: str) -> RetentionCurve:
        """Fetch the retention curve (view_pct 0..1 per time segment)."""
        resp = self.http.get(
            _RETENTION_URL,
            params={
                "ids": "channel==MINE",
                "metrics": "audienceWatchRatio",
                "filters": f"video=={video_id}",
                "dimensions": "elapsedVideoTimeRatio",
            },
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        if resp.status_code != 200:
            raise AnalyticsError(
                "retention pull failed",
                details={"status": resp.status_code, "body": resp.text[:300]},
            )
        rows = resp.json().get("rows", [])
        segments: list[tuple[int, float]] = []
        for row in rows:
            if len(row) >= 2:
                ratio = float(row[0])
                pct = float(row[1])
                segments.append((int(ratio * 100), max(0.0, min(1.0, pct))))
        if not segments:
            raise AnalyticsError(f"no retention data for {video_id}")
        return RetentionCurve(video_id=video_id, segments=segments)

    # -- ingest -------------------------------------------------------------

    def ingest_all(self, universe_id: str, workspace_dir: Path) -> int:
        """Pull stats + retention for every published video of a universe.

        Returns the number of videos ingested (skips cached ones).
        """
        videos = self._published_videos(universe_id, workspace_dir)
        ingested = 0
        for video_id, project, channel in videos:
            if self._cached(video_id):
                continue
            try:
                stats = self.pull_video_stats(video_id, project=project, channel=channel)
                curve = self.pull_retention(video_id)
            except AnalyticsError as exc:
                logger.warning("analytics pull failed", video=video_id, error=str(exc))
                continue
            self._save_stats(stats)
            self._save_retention(curve)
            ingested += 1
        return ingested

    def _published_videos(
        self, universe_id: str, workspace_dir: Path
    ) -> list[tuple[str, str, str]]:
        """Scan workspace projects for publish receipts matching the universe."""
        if not workspace_dir.exists():
            return []
        out: list[tuple[str, str, str]] = []
        for project_dir in sorted(p for p in workspace_dir.iterdir() if p.is_dir()):
            project = project_dir.name
            receipt = load_receipt(project_dir / "07_video" / "publish.json")
            if receipt is None or receipt.video_id in ("", "dry-run"):
                continue
            if not self._project_in_universe(project_dir, universe_id):
                continue
            out.append((receipt.video_id, project, ""))
        return out

    @staticmethod
    def _project_in_universe(project_dir: Path, universe_id: str) -> bool:
        from storyforge.core.types import Story

        try:
            story = Story.model_validate_json(
                (project_dir / "04_story" / "story.json").read_text(encoding="utf-8")
            )
        except (ValueError, OSError):
            return False
        return story.config.universe == universe_id

    def _cached(self, video_id: str) -> bool:
        path = self.warehouse_dir / f"{video_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        pulled = data.get("pulled_at", "")
        if not pulled:
            return False
        try:
            pulled_dt = datetime.fromisoformat(str(pulled))
        except ValueError:
            return False
        ttl_seconds = self.cache_ttl_hours * 3600
        return (datetime.now(tz=UTC) - pulled_dt).total_seconds() < ttl_seconds

    # -- persistence ---------------------------------------------------------

    def _save_stats(self, stats: VideoStats) -> None:
        path = self.warehouse_dir / f"{stats.video_id}.json"
        self._atomic_write(path, stats.model_dump_json(indent=2))

    def _save_retention(self, curve: RetentionCurve) -> None:
        path = self.retention_dir / f"{curve.video_id}.json"
        self._atomic_write(path, curve.model_dump_json(indent=2))

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def load_stats(self, video_id: str) -> VideoStats | None:
        path = self.warehouse_dir / f"{video_id}.json"
        if not path.exists():
            return None
        return VideoStats.model_validate_json(path.read_text(encoding="utf-8"))

    def load_retention(self, video_id: str) -> RetentionCurve | None:
        path = self.retention_dir / f"{video_id}.json"
        if not path.exists():
            return None
        return RetentionCurve.model_validate_json(path.read_text(encoding="utf-8"))


# -- retention → scene mapping --------------------------------------------------


def map_retention_to_scenes(
    curve: RetentionCurve,
    clips: list[NarrationClip],
) -> list[SceneRetention]:
    """Cut the per-second retention curve at each clip boundary.

    Each scene's retention is the mean of the segments that fall inside its
    narration window (per NarrationClip.duration).
    """
    total_duration = sum(c.duration_seconds for c in clips)
    if total_duration <= 0 or not curve.segments:
        return []

    result: list[SceneRetention] = []
    cursor = 0.0
    for clip in clips:
        start_pct = cursor / total_duration
        end_pct = (cursor + clip.duration_seconds) / total_duration
        inside = [
            pct
            for idx, pct in curve.segments
            if start_pct <= idx / 100.0 <= end_pct
        ]
        avg = sum(inside) / len(inside) if inside else 0.0
        result.append(
            SceneRetention(
                video_id=curve.video_id,
                scene_id=clip.scene_id,
                avg_view_pct=avg,
            )
        )
        cursor += clip.duration_seconds
    return result
