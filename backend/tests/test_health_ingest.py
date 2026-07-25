"""Health fusion-ingest endpoint tests (Issue #9).

Covers the sportsperformance-only write gate, audit events on every ingest,
range validation, and the no-diagnosis-text guarantee on injury history.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from fastapi.testclient import TestClient

BASE = "/api/v1/health-workload/ingest"

ALLOWED = [UserRole.admin, UserRole.sportsperformance]
DENIED = [UserRole.analyst, UserRole.coach, UserRole.player, UserRole.viewer]


def _make_user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _override_db() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None  # no existing row → insert
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[get_db] = _db


def _cleanup() -> None:
    app.dependency_overrides.clear()


def _wellness_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "player_id": str(uuid.uuid4()),
        "date": "2026-07-01",
        "soreness": 4,
        "sleep_hours": 7.5,
        "energy": 6,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("role", ALLOWED)
def test_ingest_wellness_allowed_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.post(f"{BASE}/wellness", json={"rows": [_wellness_row()]})
    finally:
        _cleanup()
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"upserted": 1, "source": "wellness"}


@pytest.mark.parametrize("role", DENIED)
def test_ingest_wellness_denied_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.post(f"{BASE}/wellness", json={"rows": [_wellness_row()]})
    finally:
        _cleanup()
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"


def test_ingest_emits_audit_event_without_row_contents() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db()
    try:
        with (
            patch("app.routers.health_ingest.audit_event") as audit,
            TestClient(app) as c,
        ):
            resp = c.post(f"{BASE}/wellness", json={"rows": [_wellness_row()]})
    finally:
        _cleanup()
    assert resp.status_code == 201
    events = [call.args[0] for call in audit.call_args_list]
    assert "audit.health_workload.ingest.write" in events
    kwargs = audit.call_args_list[events.index("audit.health_workload.ingest.write")].kwargs
    assert kwargs["ingest_source"] == "wellness"
    assert kwargs["count"] == 1
    # Never row contents in the audit line.
    assert "soreness" not in kwargs and "sleep_hours" not in kwargs


def test_wellness_scale_out_of_range_rejected() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.admin)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.post(f"{BASE}/wellness", json={"rows": [_wellness_row(soreness=11)]})
    finally:
        _cleanup()
    assert resp.status_code == 422


def test_injury_history_rejects_diagnosis_text() -> None:
    # extra="forbid": clinical free-text can never ride along on an injury row.
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db()
    row = {
        "player_id": str(uuid.uuid4()),
        "reported_date": "2026-06-20",
        "body_region": "hamstring",
        "side": "L",
        "status": "active",
        "days_missed": 4,
        "diagnosis": "grade 2 strain",
    }
    try:
        with TestClient(app) as c:
            resp = c.post(f"{BASE}/injury-history", json={"rows": [row]})
    finally:
        _cleanup()
    assert resp.status_code == 422


def test_injury_history_coarse_row_accepted() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db()
    row = {
        "player_id": str(uuid.uuid4()),
        "reported_date": "2026-06-20",
        "body_region": "hamstring",
        "side": "L",
        "status": "active",
        "days_missed": 4,
    }
    try:
        with TestClient(app) as c:
            resp = c.post(f"{BASE}/injury-history", json={"rows": [row]})
    finally:
        _cleanup()
    assert resp.status_code == 201
    assert resp.json()["source"] == "injury_history"


def test_gps_and_strength_and_academic_ingest_accepted() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.admin)
    _override_db()
    player_id = str(uuid.uuid4())
    try:
        with TestClient(app) as c:
            gps = c.post(
                f"{BASE}/gps",
                json={
                    "rows": [
                        {
                            "player_id": player_id,
                            "date": "2026-07-01",
                            "session_type": "practice",
                            "total_distance_m": 5200.0,
                            "sprint_count": 9,
                            "player_load": 412.0,
                        }
                    ]
                },
            )
            strength = c.post(
                f"{BASE}/strength",
                json={
                    "rows": [
                        {
                            "player_id": player_id,
                            "date": "2026-07-01",
                            "session_volume": 12000.0,
                            "session_rpe": 7.0,
                        }
                    ]
                },
            )
            academic = c.post(
                f"{BASE}/academic",
                json={
                    "rows": [
                        {
                            "date": "2026-12-07",
                            "end_date": "2026-12-12",
                            "event_type": "exams",
                            "label": "Fall finals",
                        }
                    ]
                },
            )
    finally:
        _cleanup()
    assert gps.status_code == 201 and gps.json()["source"] == "gps_wearables"
    assert strength.status_code == 201 and strength.json()["source"] == "strength_conditioning"
    assert academic.status_code == 201 and academic.json()["source"] == "academic_calendar"
