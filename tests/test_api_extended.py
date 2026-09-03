"""M5-W1 extended API tests — jobs, artifacts, webhooks, rate limit, RBAC."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from storyforge.api.app import _hash_password, create_app, make_token


@pytest.fixture(autouse=True)
def env_vars():
    os.environ.setdefault("SF__LLM__API_KEY", "test-key")
    os.environ.setdefault("SF__API__JWT_SECRET", "test-secret")
    yield


@pytest.fixture()
def users_file(tmp_path: Path) -> Path:
    path = tmp_path / "users.json"
    path.write_text(
        json.dumps(
            {
                "owner@sf.dev": {"role": "owner", "password_hash": _hash_password("secret")},
                "editor@sf.dev": {"role": "editor", "password_hash": _hash_password("secret")},
                "viewer@sf.dev": {"role": "viewer", "password_hash": _hash_password("secret")},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def client(
    users_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    from storyforge.core.config import Settings

    monkeypatch.setenv("SF__API__JWT_SECRET", "test-secret")
    monkeypatch.setenv("SF__API__USERS_FILE", str(users_file))
    monkeypatch.setenv("SF__WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("SF__QUEUE__QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setenv("SF__KNOWLEDGE__KB_DATA_DIR", str(tmp_path / "kb"))
    settings = Settings()
    return TestClient(create_app(settings))


def _token(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token({'sub': role + '@sf.dev', 'role': role}, 'test-secret', 60)}"}


# -- universes ----------------------------------------------------------------


def test_universes_lists_real_dirs(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "kb" / "storyvu").mkdir(parents=True)
    (tmp_path / "kb" / "e2e").mkdir(parents=True)
    response = client.get("/universes", headers=_token("viewer"))
    assert response.status_code == 200
    assert set(response.json()["universes"]) == {"storyvu", "e2e"}


# -- jobs ---------------------------------------------------------------------


def test_post_jobs_requires_editor(client: TestClient) -> None:
    response = client.post("/jobs", json={"project": "p1"}, headers=_token("viewer"))
    assert response.status_code == 403


def test_post_jobs_enqueues(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/jobs", json={"project": "p1", "universe": "u1"}, headers=_token("editor")
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    queue_files = list((tmp_path / "queue").glob("*.yaml"))
    assert any(job_id in p.name for p in queue_files)


def test_post_jobs_missing_project_422(client: TestClient) -> None:
    response = client.post("/jobs", json={}, headers=_token("editor"))
    assert response.status_code == 422


def test_post_jobs_idempotency(client: TestClient) -> None:
    headers = {**_token("editor"), "Idempotency-Key": "job-key-1"}
    first = client.post("/jobs", json={"project": "p1"}, headers=headers)
    second = client.post("/jobs", json={"project": "p1"}, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["status"] == "replayed"


def test_get_jobs_buckets(client: TestClient, tmp_path: Path) -> None:
    client.post("/jobs", json={"project": "p1"}, headers=_token("editor"))
    response = client.get("/jobs", headers=_token("viewer"))
    assert response.status_code == 200
    assert len(response.json()["queued"]) == 1


def test_get_job_unknown_404(client: TestClient) -> None:
    response = client.get("/jobs/nope", headers=_token("viewer"))
    assert response.status_code == 404


# -- project artifacts --------------------------------------------------------


def test_get_cost_empty_manifest(client: TestClient) -> None:
    response = client.get("/projects/p1/cost", headers=_token("viewer"))
    assert response.status_code == 200
    assert response.json()["project"] == "p1"


def test_get_lint_missing_404(client: TestClient) -> None:
    response = client.get("/projects/p1/lint", headers=_token("viewer"))
    assert response.status_code == 404


def test_get_manifest_present(client: TestClient, tmp_path: Path) -> None:
    ws = tmp_path / "ws" / "p1"
    ws.mkdir(parents=True)
    (ws / "manifest.json").write_text(json.dumps({"project": "p1"}), encoding="utf-8")
    response = client.get("/projects/p1/manifest", headers=_token("viewer"))
    assert response.status_code == 200
    assert response.json()["project"] == "p1"


def test_get_story_missing_404(client: TestClient) -> None:
    response = client.get("/projects/p1/story", headers=_token("viewer"))
    assert response.status_code == 404


# -- webhooks -----------------------------------------------------------------


def test_webhook_crud_requires_owner(client: TestClient) -> None:
    response = client.post(
        "/webhooks", json={"universe_id": "u1", "url": "https://hook.example.com"},
        headers=_token("editor"),
    )
    assert response.status_code == 403


def test_webhook_add_list_remove(client: TestClient) -> None:
    headers = _token("owner")
    added = client.post(
        "/webhooks", json={"universe_id": "u1", "url": "https://hook.example.com"},
        headers=headers,
    )
    assert added.status_code == 201

    listed = client.get("/webhooks?universe=u1", headers=_token("viewer"))
    assert listed.status_code == 200
    assert len(listed.json()["targets"]) == 1

    removed = client.request(
        "DELETE", "/webhooks", json={"universe_id": "u1", "url": "https://hook.example.com"},
        headers=headers,
    )
    assert removed.status_code == 200
    assert client.get("/webhooks?universe=u1", headers=_token("viewer")).json()["targets"] == []


# -- rate limit ---------------------------------------------------------------


def test_rate_limit_429(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from storyforge.core.config import Settings

    monkeypatch.setenv("SF__API__RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("SF__API__JWT_SECRET", "test-secret")
    settings = Settings()
    limited = TestClient(create_app(settings))

    for _ in range(2):
        assert limited.get("/projects", headers=_token("viewer")).status_code == 200
    assert limited.get("/projects", headers=_token("viewer")).status_code == 429
