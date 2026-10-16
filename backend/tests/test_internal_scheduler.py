"""The cron-facing tick route.

An in-process asyncio loop cannot fire on a platform that suspends an idle
container, so the nightly work is also reachable over HTTP. That makes its
authentication the whole story: an unauthenticated tick endpoint would let
anyone on the internet drive the training pipeline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from app.config import get_settings
from app.database import get_db
from app.main import app
from fastapi.testclient import TestClient

TOKEN = "test-scheduler-token"  # noqa: S105 — a fixture value, not a real secret


def _with_token(monkeypatch: Any, token: str = TOKEN) -> None:
    monkeypatch.setenv("SCHEDULER_TOKEN", token)
    get_settings.cache_clear()


def _clear(monkeypatch: Any) -> None:
    monkeypatch.delenv("SCHEDULER_TOKEN", raising=False)
    get_settings.cache_clear()


def _stub_db() -> None:
    """Override the DB dependency; the tick itself is patched out."""

    async def _session() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _session


def test_tick_runs_with_a_valid_token(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    _stub_db()
    try:
        with patch("app.routers.internal.run_locked_tick", new=AsyncMock(return_value={"a": 1})):
            with TestClient(app) as c:
                resp = c.post(
                    "/internal/scheduler/tick", headers={"Authorization": f"Bearer {TOKEN}"}
                )
        assert resp.status_code == 200
        # No CFBD key in tests, so the weekly roster refresh reports nothing to do.
        assert resp.json() == {"status": "ok", "a": 1, "cfbd_roster": None}
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_tick_reports_skipped_when_another_holds_the_lock(monkeypatch: Any) -> None:
    # Two overlapping cron deliveries are normal, not an error — the loser must
    # not look like a failure to whatever is watching the cron's exit status.
    _with_token(monkeypatch)
    _stub_db()
    try:
        with patch("app.routers.internal.run_locked_tick", new=AsyncMock(return_value=None)):
            with TestClient(app) as c:
                resp = c.post(
                    "/internal/scheduler/tick", headers={"Authorization": f"Bearer {TOKEN}"}
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_tick_rejects_a_wrong_token(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    _stub_db()
    try:
        with patch("app.routers.internal.run_locked_tick", new=AsyncMock()) as tick:
            with TestClient(app) as c:
                resp = c.post("/internal/scheduler/tick", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401
        tick.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_tick_rejects_a_missing_token(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    _stub_db()
    try:
        with TestClient(app) as c:
            assert c.post("/internal/scheduler/tick").status_code == 401
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_tick_rejects_a_non_bearer_scheme(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    _stub_db()
    try:
        with TestClient(app) as c:
            resp = c.post("/internal/scheduler/tick", headers={"Authorization": f"Basic {TOKEN}"})
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_tick_is_invisible_when_no_token_is_configured(monkeypatch: Any) -> None:
    # An operator who has not set SCHEDULER_TOKEN has not opted into this
    # surface; 404 rather than 401 so it does not advertise itself.
    _clear(monkeypatch)
    _stub_db()
    try:
        with TestClient(app) as c:
            resp = c.post("/internal/scheduler/tick", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_tick_route_is_not_in_the_public_schema(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    try:
        with TestClient(app) as c:
            schema = c.get("/openapi.json").json()
        assert not any(p.startswith("/internal") for p in schema.get("paths", {}))
    finally:
        _clear(monkeypatch)


def test_runtime_activity_requires_scheduler_token(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    _stub_db()
    try:
        with TestClient(app) as client:
            assert client.get("/internal/runtime/activity").status_code == 401
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_runtime_activity_only_retains_live_hosted_leases(monkeypatch: Any) -> None:
    from sqlalchemy.dialects import postgresql

    _with_token(monkeypatch)
    database = AsyncMock()
    database.scalar.return_value = True

    async def session() -> Any:
        yield database

    app.dependency_overrides[get_db] = session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/internal/runtime/activity", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        assert response.status_code == 200
        assert response.json() == {"active": True}
        query = database.scalar.call_args.args[0].compile(dialect=postgresql.dialect())
        assert "lease_expires_at >" in str(query)
        assert "cloudflare-cpu-primary" in query.params.values()
        assert "running" in query.params.values()
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)


def test_runtime_recovery_is_scoped_to_hosted_worker(monkeypatch: Any) -> None:
    _with_token(monkeypatch)
    _stub_db()
    try:
        with patch(
            "app.routers.internal.recover_shutdown_pauses", new=AsyncMock(return_value=2)
        ) as recover:
            with TestClient(app) as client:
                response = client.post(
                    "/internal/runtime/recover", headers={"Authorization": f"Bearer {TOKEN}"}
                )
        assert response.status_code == 200
        assert response.json() == {"recovered": 2}
        assert recover.call_args.kwargs == {"worker_id": "cloudflare-cpu-primary"}
    finally:
        app.dependency_overrides.clear()
        _clear(monkeypatch)
