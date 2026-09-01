"""M7-V3: analytics ingestion unit tests — mock YouTube API."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from storyforge.analytics.ingest import (
    AnalyticsError,
    AnalyticsIngestor,
    RetentionCurve,
    SceneRetention,
    VideoStats,
    map_retention_to_scenes,
)
from storyforge.core.types import NarrationClip


@pytest.fixture()
def warehouse(tmp_path: Path) -> Path:
    return tmp_path / "analytics"


def _mock_transport(*, stats_ok: bool = True, retention_ok: bool = True) -> httpx.Client:
    class _Mock(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)

            if "videos" in url and "statistics" in url:
                if not stats_ok:
                    return httpx.Response(500, request=request)
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "vid123",
                                "statistics": {
                                    "viewCount": "1500",
                                    "averageViewPercentage": "45.0",
                                },
                                "snippet": {"channelTitle": "My Channel"},
                            }
                        ]
                    },
                    request=request,
                )

            if "youtubeAnalytics" in url:
                if not retention_ok:
                    return httpx.Response(500, request=request)
                return httpx.Response(
                    200,
                    json={
                        "rows": [
                            [0.0, 1.0],
                            [0.1, 0.9],
                            [0.2, 0.8],
                            [0.3, 0.7],
                            [0.4, 0.6],
                            [0.5, 0.5],
                            [0.6, 0.4],
                            [0.7, 0.3],
                            [0.8, 0.2],
                            [0.9, 0.1],
                            [1.0, 0.0],
                        ]
                    },
                    request=request,
                )

            return httpx.Response(200, json={}, request=request)

    return httpx.Client(transport=_Mock())


# -- pull_video_stats ---------------------------------------------------------


def test_pull_video_stats(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(warehouse, access_token="t", http=_mock_transport())
    stats = ingestor.pull_video_stats("vid123", project="p1")
    assert stats.video_id == "vid123"
    assert stats.views == 1500
    assert stats.avg_view_pct == 45.0
    assert stats.project == "p1"
    assert stats.channel == "My Channel"


def test_pull_video_stats_failure(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(
        warehouse, access_token="t", http=_mock_transport(stats_ok=False)
    )
    with pytest.raises(AnalyticsError, match="stats pull failed"):
        ingestor.pull_video_stats("vid123")


def test_pull_video_stats_not_found(warehouse: Path) -> None:
    class _NotFound(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": []}, request=request)

    ingestor = AnalyticsIngestor(
        warehouse, access_token="t", http=httpx.Client(transport=_NotFound())
    )
    with pytest.raises(AnalyticsError, match="video not found"):
        ingestor.pull_video_stats("missing")


# -- pull_retention -----------------------------------------------------------


def test_pull_retention(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(warehouse, access_token="t", http=_mock_transport())
    curve = ingestor.pull_retention("vid123")
    assert curve.video_id == "vid123"
    assert len(curve.segments) == 11
    assert curve.segments[0] == (0, 1.0)
    assert curve.segments[-1] == (100, 0.0)


def test_pull_retention_failure(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(
        warehouse, access_token="t", http=_mock_transport(retention_ok=False)
    )
    with pytest.raises(AnalyticsError, match="retention pull failed"):
        ingestor.pull_retention("vid123")


# -- save/load roundtrip ------------------------------------------------------


def test_save_and_load_stats(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(warehouse, access_token="t", http=_mock_transport())
    stats = VideoStats(video_id="v1", views=500)
    ingestor._save_stats(stats)
    loaded = ingestor.load_stats("v1")
    assert loaded is not None
    assert loaded.views == 500


def test_load_stats_missing(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(warehouse, access_token="t")
    assert ingestor.load_stats("missing") is None


def test_save_and_load_retention(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(warehouse, access_token="t")
    curve = RetentionCurve(video_id="v1", segments=[(0, 1.0), (50, 0.5)])
    ingestor._save_retention(curve)
    loaded = ingestor.load_retention("v1")
    assert loaded is not None
    assert len(loaded.segments) == 2


# -- cache -------------------------------------------------------------------


def test_cached_returns_true_within_ttl(warehouse: Path) -> None:

    ingestor = AnalyticsIngestor(warehouse, access_token="t", cache_ttl_hours=24)
    stats = VideoStats(video_id="v1", views=100)
    ingestor._save_stats(stats)
    assert ingestor._cached("v1") is True


def test_cached_returns_false_when_missing(warehouse: Path) -> None:
    ingestor = AnalyticsIngestor(warehouse, access_token="t")
    assert ingestor._cached("missing") is False


# -- retention → scene mapping ------------------------------------------------


def test_map_retention_to_scenes() -> None:
    curve = RetentionCurve(
        video_id="v1",
        segments=[(0, 1.0), (25, 0.8), (50, 0.6), (75, 0.4), (100, 0.2)],
    )
    clips = [
        NarrationClip(scene_id="s0", audio_path=Path("s0.mp3"), duration_seconds=10, char_count=50),
        NarrationClip(scene_id="s1", audio_path=Path("s1.mp3"), duration_seconds=10, char_count=60),
        NarrationClip(scene_id="s2", audio_path=Path("s2.mp3"), duration_seconds=10, char_count=40),
        NarrationClip(scene_id="s3", audio_path=Path("s3.mp3"), duration_seconds=10, char_count=45),
    ]
    scenes = map_retention_to_scenes(curve, clips)
    assert len(scenes) == 4
    assert scenes[0].scene_id == "s0"
    assert scenes[0].avg_view_pct > 0.7  # early scenes have higher retention
    assert scenes[3].scene_id == "s3"
    assert scenes[3].avg_view_pct < 0.5  # later scenes have lower retention


def test_map_retention_empty_clips() -> None:
    curve = RetentionCurve(video_id="v1", segments=[(0, 1.0)])
    assert map_retention_to_scenes(curve, []) == []


def test_map_retention_no_segments() -> None:
    clip = NarrationClip(scene_id="s0", audio_path=Path("s0.mp3"), duration_seconds=10, char_count=50)
    curve = RetentionCurve(video_id="v1", segments=[])
    assert map_retention_to_scenes(curve, [clip]) == []


# -- SceneRetention model -----------------------------------------------------


def test_scene_retention_model() -> None:
    sr = SceneRetention(video_id="v1", scene_id="s0", avg_view_pct=0.85)
    assert sr.avg_view_pct == 0.85
