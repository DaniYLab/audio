"""M4-A4: YouTube upload via the resumable Data API v3 (httpx, no heavy SDK).

Usage::

    uploader = YouTubeUploader(token_path, client_id, client_secret, refresh_token)
    video_id = uploader.upload(video_path, thumbnail_path, metadata)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from storyforge.core.exceptions import StoryForgeError
from storyforge.publish.metadata import PublishMetadata

# YouTube Data API v3 endpoints
_UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3/videos"
_API_BASE = "https://www.googleapis.com/youtube/v3/videos"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB — YouTube's recommended minimum


class YouTubeUploadError(StoryForgeError):
    """Upload failed (network, quota, auth, …)."""


class YouTubeUploader:
    """YouTube upload via httpx with OAuth2 token refresh.

    ``http`` is optional — a client is created on demand when ``upload()`` is
    called. Pass a mock ``http`` in tests.
    """

    def __init__(
        self,
        token_path: Path,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        http: httpx.Client | None = None,
    ) -> None:
        self.token_path = token_path
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._http = http

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0))
        return self._http

    # -- OAuth ---------------------------------------------------------------

    def _access_token(self) -> str:
        """Obtain a fresh access token from the refresh token.

        The result is cached in ``self.token_path`` for reuse across uploads.
        """
        cached = self._load_token()
        if cached and float(str(cached.get("expires_at", 0))) > time.time() + 60:
            return str(cached["access_token"])

        resp = self.http.post(
            _TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise YouTubeUploadError(
                "token refresh failed",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        body = resp.json()
        access_token = str(body["access_token"])
        expires_in = int(body.get("expires_in", 3600))
        self._save_token(access_token, expires_in)
        return access_token

    def _load_token(self) -> dict[str, object]:
        if not self.token_path.exists():
            return {}
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            return dict(data) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_token(self, access_token: str, expires_in: int) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps(
                {
                    "access_token": access_token,
                    "expires_at": time.time() + expires_in,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # -- upload --------------------------------------------------------------

    def upload(
        self,
        video_path: Path,
        thumbnail_path: Path | None,
        meta: PublishMetadata,
    ) -> str:
        """Upload a video via the resumable protocol.

        Returns the YouTube video id on success.
        """
        if not video_path.exists():
            raise YouTubeUploadError(f"video not found: {video_path}")

        access_token = self._access_token()
        video_size = video_path.stat().st_size

        # Step 1: initiate resumable upload session.
        upload_url = self._initiate(access_token, meta, video_size)

        # Step 2: upload the video bytes in chunks.
        video_id = self._upload_bytes(upload_url, video_path, video_size, access_token)

        # Step 3: set thumbnail (best-effort, not a hard error).
        if thumbnail_path and thumbnail_path.exists():
            self._set_thumbnail(access_token, video_id, thumbnail_path)

        return video_id

    def _initiate(self, access_token: str, meta: PublishMetadata, video_size: int) -> str:
        """POST the metadata JSON to start a resumable session.

        Returns the ``Location`` header (upload URL).
        """
        body = {
            "snippet": {
                "title": meta.title,
                "description": meta.description,
                "tags": meta.tags,
                "categoryId": meta.category_id,
            },
            "status": {
                "privacyStatus": meta.privacy,
                "selfDeclaredMadeForKids": meta.make_for_kids,
            },
        }
        resp = self.http.post(
            _UPLOAD_BASE,
            params={"part": "snippet,status", "uploadType": "resumable"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(video_size),
            },
            json=body,
        )
        if resp.status_code == 401:
            # Token expired — invalidate cache and retry once with a fresh one.
            self._save_token("", 0)
            return self._initiate(self._access_token(), meta, video_size)
        if resp.status_code == 403:
            if "quota" in resp.text.lower():
                raise YouTubeUploadError("quota exceeded — try again later")
            raise YouTubeUploadError(
                "upload initiation failed (403)", details={"body": resp.text[:500]}
            )
        if resp.status_code != 200:
            raise YouTubeUploadError(
                "upload initiation failed",
                details={"status": resp.status_code, "body": resp.text[:500]},
            )
        location = resp.headers.get("Location") or ""
        if not location:
            raise YouTubeUploadError("no Location header in initiation response")
        return location

    def _upload_bytes(
        self,
        upload_url: str,
        video_path: Path,
        video_size: int,
        access_token: str,
    ) -> str:
        """Upload the video file in chunks (resumable).

        Returns the YouTube video id from the final chunk response.
        """
        final_resp: httpx.Response | None = None
        with video_path.open("rb") as fh:
            uploaded = 0
            while uploaded < video_size:
                fh.seek(uploaded)
                chunk = fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                chunk_end = min(uploaded + len(chunk), video_size) - 1
                resp = self.http.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {uploaded}-{chunk_end}/{video_size}",
                        "Authorization": f"Bearer {access_token}",
                    },
                )
                if resp.status_code == 401:
                    # Token expired mid-upload — refresh and retry the same chunk.
                    self._save_token("", 0)
                    access_token = self._access_token()
                    continue
                if resp.status_code not in (308, 200, 201):
                    raise YouTubeUploadError(
                        "chunk upload failed",
                        details={
                            "status": resp.status_code,
                            "body": resp.text[:500],
                        },
                    )
                uploaded += len(chunk)
                final_resp = resp

        if final_resp is None:
            raise YouTubeUploadError("no chunk uploaded — empty video?")
        return self._extract_video_id(final_resp)

    @staticmethod
    def _extract_video_id(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            video_id = str(data.get("id", ""))
            if not video_id:
                raise YouTubeUploadError("empty video id in upload response")
            return video_id
        except (json.JSONDecodeError, ValueError) as exc:
            raise YouTubeUploadError(
                "cannot parse upload response", details={"body": resp.text[:500]}
            ) from exc

    # -- thumbnail -----------------------------------------------------------

    def _set_thumbnail(
        self, access_token: str, video_id: str, thumbnail_path: Path
    ) -> None:
        """Set the video thumbnail (best-effort, never a hard error)."""
        url = f"{_API_BASE}/setThumbnail"
        try:
            with thumbnail_path.open("rb") as fh:
                resp = self.http.post(
                    url,
                    params={"videoId": video_id},
                    headers={"Authorization": f"Bearer {access_token}"},
                    files={"thumbnail": (thumbnail_path.name, fh, "image/png")},
                )
            if resp.status_code not in (200, 201):
                pass  # best-effort
        except Exception:
            pass  # best-effort


# -- OAuth setup helper --------------------------------------------------------


def build_oauth_url(client_id: str, redirect_uri: str = "http://localhost:8080") -> str:
    """Build the OAuth consent URL for the initial authorization flow."""
    params = (
        f"client_id={client_id}",
        f"redirect_uri={redirect_uri}",
        "scope=https://www.googleapis.com/auth/youtube.upload",
        "response_type=code",
        "access_type=offline",
        "prompt=consent",
    )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + "&".join(params)


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = "http://localhost:8080",
    http: httpx.Client | None = None,
) -> dict[str, object]:
    """Exchange an authorization code for a refresh token.

    Returns the full token response (includes ``refresh_token``, ``access_token``,
    ``expires_in``). Save the refresh token to your env / vault.
    """
    if http is None:
        http = httpx.Client()
    resp = http.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if resp.status_code != 200:
        raise YouTubeUploadError(
            "code exchange failed",
            details={"status": resp.status_code, "body": resp.text[:500]},
        )
    return dict(resp.json())
