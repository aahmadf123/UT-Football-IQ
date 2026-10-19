from __future__ import annotations

from app import error_log
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from fastapi import APIRouter
from fastapi.testclient import TestClient

boom = APIRouter()


@boom.get("/api/v1/_test/boom")
async def explode() -> None:
    raise RuntimeError("connect to postgresql://user:secret@db.example/x failed")


def test_unhandled_error_returns_json_with_an_id_admins_can_look_up():
    app.include_router(boom)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/_test/boom")
        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Something went wrong on the server."
        entry = error_log.recent()[0]
        assert entry["error_id"] == body["error_id"]
        assert entry["error_type"] == "RuntimeError"
        assert entry["path"] == "/api/v1/_test/boom"
        assert (
            entry["location"].startswith("tests/test_error_log.py")
            or "explode" in entry["location"]
        )
        assert "secret" not in entry["message"]

        app.dependency_overrides[get_current_user] = lambda: User(
            email="a@example.com", role=UserRole.admin, is_active=True
        )
        listed = client.get("/api/v1/system/errors")
        assert listed.status_code == 200
        assert listed.json()[0]["error_id"] == body["error_id"]

        app.dependency_overrides[get_current_user] = lambda: User(
            email="b@example.com", role=UserRole.analyst, is_active=True
        )
        assert client.get("/api/v1/system/errors").status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/_test/boom"
        ]
