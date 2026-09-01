"""M5-W1 API tests — TestClient auth + read-only endpoints (no real network)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from storyforge.api.app import _hash_password, create_app, make_token, verify_token


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
            {"admin@storyforge.dev": {"role": "owner", "password_hash": _hash_password("secret")}}
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def client(users_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from storyforge.core.config import Settings

    monkeypatch.setenv("SF__API__JWT_SECRET", "test-secret")
    monkeypatch.setenv("SF__API__USERS_FILE", str(users_file))
    monkeypatch.setenv("SF__WORKSPACE_DIR", str(tmp_path / "ws"))
    settings = Settings()
    return TestClient(create_app(settings))


def test_jwt_roundtrip():
    token = make_token({"sub": "a@b.c", "role": "owner"}, "secret", 60)
    payload = verify_token(token, "secret")
    assert payload["sub"] == "a@b.c"
    assert payload["role"] == "owner"


def test_health(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_success(client: TestClient):
    response = client.post(
        "/auth/login", json={"email": "admin@storyforge.dev", "password": "secret"}
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_bad_password(client: TestClient):
    response = client.post(
        "/auth/login", json={"email": "admin@storyforge.dev", "password": "wrong"}
    )
    assert response.status_code == 401


def test_projects_requires_auth(client: TestClient):
    assert client.get("/projects").status_code == 401


def test_projects_empty(client: TestClient):
    token = make_token({"sub": "admin@storyforge.dev", "role": "owner"}, "test-secret", 60)
    response = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"projects": []}


def test_story_missing_404(client: TestClient):
    token = make_token({"sub": "admin@storyforge.dev", "role": "owner"}, "test-secret", 60)
    response = client.get("/projects/nope/story", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
