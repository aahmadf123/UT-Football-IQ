"""CFBD refresh uses server credentials, admin authorization and bounded scopes."""

import uuid
from collections.abc import AsyncGenerator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from app.cfbd.models import CFBDSyncStatus
from app.cfbd.sync import EndpointSyncResult
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from app.routers.cfbd import _sync_lock
from fastapi.testclient import TestClient


@contextmanager
def refresh_client(role=UserRole.admin):
    user = MagicMock(spec=User, id=uuid.uuid4(), role=role)
    session = AsyncMock()

    async def db() -> AsyncGenerator:
        yield session

    async def unlocked() -> AsyncGenerator:
        yield

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[_sync_lock] = unlocked
    try:
        with TestClient(app) as client:
            yield client, session
    finally:
        app.dependency_overrides.clear()


def test_connection_reports_presence_without_returning_key(monkeypatch):
    key = "test-only-vendor-credential"
    monkeypatch.setenv("CFBD_API_KEY", key)
    with refresh_client(UserRole.coach) as (client, _):
        result = client.get("/api/cfbd/status")
    assert result.status_code == 200
    assert result.json() == {"configured": True, "source": "CollegeFootballData.com"}
    assert key not in result.text


def test_non_admin_cannot_consume_vendor_quota(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("app.cfbd.client.CFBDClient.from_settings", factory)
    with refresh_client(UserRole.coach) as (client, _):
        result = client.post("/api/cfbd/sync", json={"season": 2026, "scope": "schedule"})
    assert result.status_code == 403
    factory.assert_not_called()


def test_missing_key_does_not_attempt_vendor_request(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "")
    factory = MagicMock()
    monkeypatch.setattr("app.cfbd.client.CFBDClient.from_settings", factory)
    with refresh_client() as (client, _):
        result = client.post("/api/cfbd/sync", json={"season": 2026, "scope": "schedule"})
    assert result.status_code == 503
    assert "not connected" in result.json()["detail"]
    factory.assert_not_called()


def test_plays_refresh_requires_an_explicit_bounded_week(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "test-only-key")
    factory = MagicMock()
    monkeypatch.setattr("app.cfbd.client.CFBDClient.from_settings", factory)
    with refresh_client() as (client, _):
        result = client.post("/api/cfbd/sync", json={"season": 2026, "scope": "plays"})
    assert result.status_code == 422
    factory.assert_not_called()


def test_admin_refresh_dispatches_one_week_and_returns_safe_summary(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "test-only-key")
    vendor = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = vendor
    factory = MagicMock(return_value=context)
    monkeypatch.setattr("app.cfbd.client.CFBDClient.from_settings", factory)
    sync = AsyncMock(
        return_value=EndpointSyncResult(
            endpoint="/plays",
            status=CFBDSyncStatus.succeeded,
            rows_read=17,
            rows_written=17,
        )
    )
    monkeypatch.setattr("app.cfbd.sync.sync_plays", sync)
    with refresh_client() as (client, session):
        result = client.post(
            "/api/cfbd/sync",
            json={
                "season": 2026,
                "scope": "plays",
                "week": 3,
                "season_type": "regular",
            },
        )
    assert result.status_code == 200
    assert result.json()["rows_written"] == 17
    assert result.json()["status"] == "succeeded"
    assert "test-only-key" not in result.text
    sync.assert_awaited_once_with(
        session,
        vendor,
        weeks=[3],
        season=2026,
        season_type="regular",
    )
    factory.assert_called_once_with(timeout=10.0, max_retries=1)


def test_vendor_failure_does_not_expose_errors_or_clear_cache(monkeypatch):
    monkeypatch.setenv("CFBD_API_KEY", "test-only-key")
    context = AsyncMock()
    monkeypatch.setattr("app.cfbd.client.CFBDClient.from_settings", MagicMock(return_value=context))
    monkeypatch.setattr(
        "app.cfbd.sync.sync_games",
        AsyncMock(
            return_value=EndpointSyncResult(
                endpoint="/games",
                status=CFBDSyncStatus.failed,
                error="internal vendor detail",
            )
        ),
    )
    with refresh_client() as (client, session):
        result = client.post("/api/cfbd/sync", json={"season": 2026, "scope": "schedule"})
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert "Previously cached records remain" in result.json()["message"]
    assert "internal vendor detail" not in result.text
    session.delete.assert_not_called()
