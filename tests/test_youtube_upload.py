"""M4-A4: YouTube uploader unit tests — mock httpx transport (no real API)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from storyforge.publish.metadata import PublishMetadata
from storyforge.publish.youtube import (
    YouTubeUploader,
    YouTubeUploadError,
    build_oauth_url,
    exchange_code,
)


@pytest.fixture()
def meta() -> PublishMetadata:
    return PublishMetadata(
        title="Test Video", description="A test upload.", tags=["test", "storyforge"]
    )


@pytest.fixture()
def uploader(tmp_path: Path) -> YouTubeUploader:
    return YouTubeUploader(
        token_path=tmp_path / "token.json",
        client_id="test_client_id",
        client_secret="test_client_secret",
        refresh_token="test_refresh_token",
    )


# -- mock transport -----------------------------------------------------------


def _mock_transport(
    *,
    initiate_status: int = 200,
    initiate_headers: dict[str, str] | None = None,
    chunk_status: int = 200,
    chunk_body: str | None = None,
    video_id: str = "abc123",
    token_status: int = 200,
    token_body: dict[str, Any] | None = None,
) -> httpx.Client:
    """Build a mock httpx transport that simulates the YouTube API."""

    class _MockTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            url = str(request.url)

            if "oauth2.googleapis.com/token" in url:
                if token_status != 200:
                    return httpx.Response(token_status, request=request)
                body = token_body or {
                    "access_token": "mock_access_token",
                    "expires_in": 3600,
                }
                return httpx.Response(200, json=body, request=request)

            if "/upload/youtube/v3/videos" in url and request.method == "POST":
                # Initiation response.
                headers = initiate_headers or {
                    "Location": "https://upload.googleapis.com/resumable/abc123"
                }
                return httpx.Response(
                    initiate_status, headers=dict(headers), request=request
                )

            if "/resumable/" in url and request.method == "PUT":
                # Chunk upload response (upload goes to the Location URL).
                if chunk_status == 308:
                    return httpx.Response(308, request=request)
                body = chunk_body or '{"id": "' + video_id + '"}'
                return httpx.Response(
                    chunk_status, json={"id": video_id}, text=body, request=request
                )

            if "setThumbnail" in url:
                return httpx.Response(200, request=request)

            return httpx.Response(200, json={}, request=request)

    return httpx.Client(transport=_MockTransport())


# -- upload happy path --------------------------------------------------------


def test_upload_happy_path(
    uploader: YouTubeUploader, meta: PublishMetadata, tmp_path: Path
) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1024 * 1024)  # 1 MiB
    uploader._http = _mock_transport()

    video_id = uploader.upload(video, None, meta)
    assert video_id == "abc123"


def test_upload_with_thumbnail(
    uploader: YouTubeUploader, meta: PublishMetadata, tmp_path: Path
) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1024 * 1024)
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(b"PNG")
    uploader._http = _mock_transport()

    video_id = uploader.upload(video, thumbnail, meta)
    assert video_id == "abc123"


# -- missing video ------------------------------------------------------------


def test_upload_missing_video(
    uploader: YouTubeUploader, meta: PublishMetadata, tmp_path: Path
) -> None:
    video = tmp_path / "nonexistent.mp4"
    with pytest.raises(YouTubeUploadError, match="video not found"):
        uploader.upload(video, None, meta)


# -- auth failure -------------------------------------------------------------


def test_upload_401_initiation_retry_once(
    uploader: YouTubeUploader, meta: PublishMetadata, tmp_path: Path
) -> None:
    """First call returns 401, second returns 200 → should retry once."""
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1024)

    call_count: int = 0

    class _RetryTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if "oauth2.googleapis.com/token" in str(request.url):
                return httpx.Response(
                    200, json={"access_token": "refreshed", "expires_in": 3600}, request=request
                )
            if "/upload/youtube/v3/videos" in str(request.url) and request.method == "POST":
                if call_count == 0:
                    call_count += 1
                    return httpx.Response(401, request=request)
                headers = {"Location": "https://upload.googleapis.com/resumable/abc"}
                return httpx.Response(200, headers=headers, request=request)
            if "/resumable/" in str(request.url) and request.method == "PUT":
                return httpx.Response(200, json={"id": "retried_id"}, request=request)
            return httpx.Response(200, request=request)

    uploader._http = httpx.Client(transport=_RetryTransport())
    video_id = uploader.upload(video, None, meta)
    assert video_id == "retried_id"


def test_upload_quota_exceeded(
    uploader: YouTubeUploader, meta: PublishMetadata, tmp_path: Path
) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1024)

    class _QuotaTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if "oauth2.googleapis.com/token" in str(request.url):
                return httpx.Response(
                    200, json={"access_token": "t", "expires_in": 3600}, request=request
                )
            if "/upload/youtube/v3/videos" in str(request.url) and request.method == "POST":
                return httpx.Response(403, text="quotaExceeded", request=request)
            return httpx.Response(200, request=request)

    uploader._http = httpx.Client(transport=_QuotaTransport())
    with pytest.raises(YouTubeUploadError, match="quota exceeded"):
        uploader.upload(video, None, meta)


# -- chunk upload failure -----------------------------------------------------


def test_upload_chunk_failure(
    uploader: YouTubeUploader, meta: PublishMetadata, tmp_path: Path
) -> None:
    video = tmp_path / "final.mp4"
    video.write_bytes(b"x" * 1024 * 1024)
    uploader._http = _mock_transport(chunk_status=500, chunk_body="server error")

    with pytest.raises(YouTubeUploadError, match="chunk upload failed"):
        uploader.upload(video, None, meta)


# -- token refresh ------------------------------------------------------------


def test_access_token_refresh(uploader: YouTubeUploader) -> None:
    uploader._http = _mock_transport()
    token = uploader._access_token()
    assert token == "mock_access_token"
    # Cached token file exists.
    assert uploader.token_path.exists()


def test_access_token_cached(uploader: YouTubeUploader) -> None:
    uploader._http = _mock_transport()
    first = uploader._access_token()
    second = uploader._access_token()
    assert first == second


def test_access_token_refresh_failure(uploader: YouTubeUploader) -> None:
    uploader._http = _mock_transport(token_status=400)
    with pytest.raises(YouTubeUploadError, match="token refresh failed"):
        uploader._access_token()


# -- OAuth helpers ------------------------------------------------------------


def test_build_oauth_url_contains_params() -> None:
    url = build_oauth_url("my_client_id")
    assert "client_id=my_client_id" in url
    assert "youtube.upload" in url
    assert "offline" in url
    assert "consent" in url


def test_exchange_code_success() -> None:
    http = _mock_transport(
        token_body={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        }
    )
    result = exchange_code("cid", "csecret", "code123", http=http)
    assert result["access_token"] == "at"
    assert result["refresh_token"] == "rt"


def test_exchange_code_failure() -> None:
    http = _mock_transport(token_status=400)
    with pytest.raises(YouTubeUploadError, match="code exchange failed"):
        exchange_code("cid", "csecret", "bad_code", http=http)
