"""M5-W1: REST API v1 — FastAPI app + auth + routers.

File-based for M5 (Postgres is DEV1's M5-V1): users live in a small JSON
registry (``data/secrets/users.json``), jobs enqueue onto the shared queue dir
(DEV1 M3/A6), and project artifacts are read through ``ArtifactStore``.

Endpoints (v1):
    /health                     GET   — liveness
    /auth/login                 POST  — JWT access token
    /universes                  GET   — universes on disk (owner/editor/viewer)
    /projects                   GET   — workspace projects
    /projects/{p}/story         GET   — story.json
    /projects/{p}/cost          GET   — per-stage cost report
    /projects/{p}/lint          GET   — lint_report.json
    /projects/{p}/review        GET   — review.json
    /projects/{p}/manifest      GET   — run manifest
    /jobs                       GET   — queue buckets
    /jobs                       POST  — enqueue a pipeline job (editor+)
    /jobs/{job_id}              GET   — job status (+ error when failed)
    /webhooks                   GET   — registered targets
    /webhooks                   POST  — register a target (owner)
    /webhooks                   DELETE— remove a target (owner)

Rate limiting: in-memory token bucket per user (``SF__API__RATE_LIMIT_PER_MINUTE``).
Job idempotency: an optional ``Idempotency-Key`` header replays the prior job.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable
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


# --- rate limiting (per-user token bucket, in-memory) ------------------------


class _TokenBucket:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
        self._count = 0

    def take(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 60.0:
                self._window_start = now
                self._count = 0
            if self._count >= self.per_minute:
                return False
            self._count += 1
            return True


# --- app factory -------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="StoryForge API", version="0.1.0", docs_url="/docs")

    app.state.settings = settings
    app.state.users = load_users(settings.api.users_file)
    _rate_limiters: dict[str, _TokenBucket] = {}
    _rate_lock = threading.Lock()
    # Idempotency: Idempotency-Key -> job_id (in-memory for v1).
    app.state.idempotency = {}

    def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
        token = authorization.split(" ", 1)[1]
        return verify_token(token, settings.api.jwt_secret.get_secret_value())

    def require_role(payload: dict[str, Any], min_role: str) -> None:
        rank = {"owner": 3, "editor": 2, "viewer": 1}
        if rank.get(str(payload.get("role", "viewer")), 1) < rank.get(min_role, 1):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    def auth_dep(min_role: str = "viewer") -> Callable[..., dict[str, Any]]:
        """Dependency factory: authenticate + RBAC + per-user rate limit."""

        def dep(
            authorization: Annotated[str | None, Header()] = None,
        ) -> dict[str, Any]:
            payload = current_user(authorization)
            require_role(payload, min_role)
            user = str(payload.get("sub", "anonymous"))
            with _rate_lock:
                bucket = _rate_limiters.get(user)
                if bucket is None:
                    bucket = _TokenBucket(settings.api.rate_limit_per_minute)
                    _rate_limiters[user] = bucket
            if not bucket.take():
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded"
                )
            return payload

        return dep

    def webhook_store() -> Any:
        from storyforge.notify.webhook import WebhookStore

        return WebhookStore(Path(settings.workspace_dir).parent / "webhooks")

    def queue_manager() -> Any:
        from storyforge.queue import QueueManager

        return QueueManager(settings)

    # -- health / auth ------------------------------------------------------

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

    # -- universes / projects (read) -----------------------------------------

    @app.get("/universes")
    def list_universes(
        auth: Annotated[dict[str, Any], Depends(auth_dep())],
    ) -> dict[str, Any]:
        kb_root = Path(settings.knowledge.kb_data_dir)
        universes = sorted(p.name for p in kb_root.iterdir() if p.is_dir()) if kb_root.exists() else []
        return {"user": auth.get("sub"), "role": auth.get("role"), "universes": universes}

    @app.get("/projects")
    def list_projects(
        auth: Annotated[dict[str, Any], Depends(auth_dep())],
    ) -> dict[str, list[str]]:
        root = Path(settings.workspace_dir)
        if not root.exists():
            return {"projects": []}
        return {"projects": sorted(p.name for p in root.iterdir() if p.is_dir())}

    def _store(project: str) -> Any:
        from storyforge.core.artifacts import ArtifactStore

        return ArtifactStore(settings.workspace_dir, project)

    def _artifact_or_404(path: Path, kind: str) -> dict[str, Any]:
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{kind} not found")
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=500, detail=f"{kind} corrupted") from exc

    @app.get("/projects/{project}/story")
    def get_story(
        project: str, auth: Annotated[dict[str, Any], Depends(auth_dep())]
    ) -> dict[str, Any]:
        store = _store(project)
        path = store.story_path()
        if not path.exists():
            raise HTTPException(status_code=404, detail="story not found")
        story = Story.model_validate_json(path.read_text(encoding="utf-8"))
        return story.model_dump(mode="json")

    @app.get("/projects/{project}/cost")
    def get_cost(
        project: str, auth: Annotated[dict[str, Any], Depends(auth_dep())]
    ) -> dict[str, Any]:
        from storyforge.core.cost import build_cost_report

        store = _store(project)
        manifest = store.load_manifest()
        return build_cost_report(manifest, settings).model_dump(mode="json")

    @app.get("/projects/{project}/lint")
    def get_lint(
        project: str, auth: Annotated[dict[str, Any], Depends(auth_dep())]
    ) -> dict[str, Any]:
        return _artifact_or_404(_store(project).dir("04_story") / "lint_report.json", "lint report")

    @app.get("/projects/{project}/review")
    def get_review(
        project: str, auth: Annotated[dict[str, Any], Depends(auth_dep())]
    ) -> dict[str, Any]:
        return _artifact_or_404(_store(project).dir("04_story") / "review.json", "review")

    @app.get("/projects/{project}/manifest")
    def get_manifest(
        project: str, auth: Annotated[dict[str, Any], Depends(auth_dep())]
    ) -> dict[str, Any]:
        store = _store(project)
        path = store.root / "manifest.json"
        return _artifact_or_404(path, "manifest")

    # -- jobs (queue) --------------------------------------------------------

    @app.get("/jobs")
    def list_jobs(auth: Annotated[dict[str, Any], Depends(auth_dep())]) -> dict[str, list[str]]:
        manager = queue_manager()
        return {
            "queued": [p.stem for p in manager.queued()],
            "processing": [p.stem for p in manager.processing()],
            "done": [p.stem for p in manager.done()],
            "failed": [p.stem for p in manager.failed()],
        }

    @app.post("/jobs", status_code=202)
    def enqueue_job(
        body: dict[str, Any],
        idempotency_key: Annotated[str | None, Header()] = None,
        auth: Annotated[dict[str, Any] | None, Depends(auth_dep("editor"))] = None,
    ) -> dict[str, str]:
        from storyforge.queue import new_job

        project = str(body.get("project", "")).strip()
        if not project:
            raise HTTPException(status_code=422, detail="project is required")
        manager = queue_manager()

        if idempotency_key:
            existing = app.state.idempotency.get(idempotency_key)
            if existing:
                return {"job_id": existing, "status": "replayed"}

        job = new_job(
            project,
            source_config=str(body.get("source_config", "config/story_config.example.yaml")),
            universe=str(body.get("universe", "")),
            urls=[str(u) for u in body.get("urls", [])],
            local_files=[str(f) for f in body.get("local_files", [])],
            license=str(body.get("license", "unknown")),
        )
        manager.enqueue(job)
        if idempotency_key:
            app.state.idempotency[idempotency_key] = job.id
        return {"job_id": job.id, "status": "queued"}

    @app.get("/jobs/{job_id}")
    def get_job(
        job_id: str, auth: Annotated[dict[str, Any], Depends(auth_dep())]
    ) -> dict[str, Any]:
        manager = queue_manager()
        buckets = {
            "queued": manager.queue_dir,
            "processing": manager.processing_dir,
            "done": manager.done_dir,
            "failed": manager.failed_dir,
        }
        for bucket, dir_ in buckets.items():
            path = dir_ / f"{job_id}.yaml"
            if path.exists():
                error = None
                error_path = path.with_suffix(".error.txt")
                if error_path.exists():
                    error = error_path.read_text(encoding="utf-8")
                return {"job_id": job_id, "status": bucket, "error": error}
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")

    # -- webhooks ------------------------------------------------------------

    @app.get("/webhooks")
    def list_webhooks(
        universe: str | None = None,
        auth: Annotated[dict[str, Any] | None, Depends(auth_dep())] = None,
    ) -> dict[str, Any]:
        targets = webhook_store().list_targets(universe)
        return {"targets": [t.model_dump(mode="json") for t in targets]}

    @app.post("/webhooks", status_code=201)
    def add_webhook(
        body: dict[str, str],
        auth: Annotated[dict[str, Any] | None, Depends(auth_dep("owner"))] = None,
    ) -> dict[str, str]:
        from storyforge.notify.webhook import WebhookTarget

        universe = str(body.get("universe_id", "")).strip()
        url = str(body.get("url", "")).strip()
        if not universe or not url.startswith("http"):
            raise HTTPException(status_code=422, detail="universe_id and http(s) url required")
        store = webhook_store()
        store.add_target(WebhookTarget(universe_id=universe, url=url))
        return {"universe_id": universe, "url": url, "status": "added"}

    @app.delete("/webhooks")
    def remove_webhook(
        body: dict[str, str],
        auth: Annotated[dict[str, Any] | None, Depends(auth_dep("owner"))] = None,
    ) -> dict[str, str]:
        universe = str(body.get("universe_id", "")).strip()
        url = str(body.get("url", "")).strip()
        if not webhook_store().remove_target(universe, url):
            raise HTTPException(status_code=404, detail="no matching webhook")
        return {"universe_id": universe, "url": url, "status": "removed"}

    return app


def main(host: str = "127.0.0.1", port: int = 8000, config: Path | None = None) -> None:
    """Run the API server (uvicorn) — entrypoint for ``storyforge api``."""
    import uvicorn

    settings = Settings.load(config)
    uvicorn.run(create_app(settings), host=host, port=port)
