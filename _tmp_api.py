import os
os.environ.setdefault("SF__LLM__API_KEY", "test-key")
os.environ.setdefault("SF__API__JWT_SECRET", "test-secret")
from fastapi.testclient import TestClient
from storyforge.api.app import create_app
from storyforge.core.config import Settings

c = TestClient(create_app(Settings()))
r = c.get("/projects")
print("status:", r.status_code)
print("body:", r.text[:500])