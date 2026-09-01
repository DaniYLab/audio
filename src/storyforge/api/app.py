"""M5-W1: REST API v1 — FastAPI app + auth + routers.

File-based for M5 (Postgres is DEV1's M5-V1): users live in a small JSON
registry (``data/secrets/users.json``), jobs enqueue onto the shared queue dir
(DEV1 M3/A6), and project artifacts are read through ``ArtifactStore``.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status

from storyforge.core.config import Settings
from storyforge.core.types import Story

# --- JWT (HMAC-SHA256, no external deps) -------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(header_b64: str, payload_b64: str, secret: str) -> str:
    message = f"{header_b64}.{payload_b64}".encode()
    return _b64(hmac.new(secret.encode(), message, hashlib.sha256).digest())


def make_token(payload: dict[str, Any], secret: str, ttl_minutes: int) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_minutes * 60
    payload_b64 = _b64(json.dumps(body).encode())
    return f"{header}.{payload_b64}.{_sign(header, payload_b64, secret)}"


def verify_token(token: str, secret: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, signature = token.split(".")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="malformed token"
        ) from None
    expected = _sign(header_b64, payload_b64, secret)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")
    try:
        payload_raw = json.loads(_unb64(payload_b64))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bad payload"
        ) from None
    if not isinstance(payload_raw, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bad payload"
        ) from None
    payload: dict[str, Any] = payload_raw
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired")
    return payload


# --- user registry (file-based dev store) ------------------------------------


@dataclass
class User:
    email: str
    role: str  # owner | editor | viewer
    password_hash: str


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def load_users(users_file: Path) -> dict[str, User]:
    if not users_file.exists():
        return {}
    try:
        raw = json.loads(users_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    users: dict[str, User] = {}
    for email, data in raw.items():
        if isinstance(data, dict):
            users[email] = User(
                email=email,
                role=str(data.get("role", "viewer")),
                password_hash=str(data.get("password_hash", "")),
            )
    return users


# --- app factory -------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="StoryForge API", version="0.1.0", docs_url="/docs")

    app.state.settings = settings
    app.state.users = load_users(settings.api.users_file)

    def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
        token = authorization.split(" ", 1)[1]
        return verify_token(token, settings.api.jwt_secret.get_secret_value())

    def require_role(payload: dict[str, Any], min_role: str) -> None:
        rank = {"owner": 3, "editor": 2, "viewer": 1}
        if rank.get(str(payload.get("role", "viewer")), 1) < rank.get(min_role, 1):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/auth/login")
    def login(body: dict[str, str]) -> dict[str, str]:
        email = body.get("email", "")
        password = body.get("password", "")
        user = app.state.users.get(email)
        if user is None or user.password_hash != _hash_password(password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad credentials")
        token = make_token(
            {"sub": email, "role": user.role},
            settings.api.jwt_secret.get_secret_value(),
            settings.api.jwt_ttl_minutes,
        )
        return {"access_token": token, "token_type": "bearer"}

    @app.get("/universes")
    def list_universes(auth: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
        return {"user": auth.get("sub"), "role": auth.get("role")}

    @app.get("/projects")
    def list_projects(
        auth: Annotated[dict[str, Any], Depends(current_user)],
    ) -> dict[str, list[str]]:
        root = Path(settings.workspace_dir)
        if not root.exists():
            return {"projects": []}
        return {"projects": sorted(p.name for p in root.iterdir() if p.is_dir())}

    @app.get("/projects/{project}/story")
    def get_story(
        project: str, auth: Annotated[dict[str, Any], Depends(current_user)]
    ) -> dict[str, Any]:
        from storyforge.core.artifacts import ArtifactStore

        store = ArtifactStore(settings.workspace_dir, project)
        path = store.story_path()
        if not path.exists():
            raise HTTPException(status_code=404, detail="story not found")
        story = Story.model_validate_json(path.read_text(encoding="utf-8"))
        return story.model_dump(mode="json")

    return app
