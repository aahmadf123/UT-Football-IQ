"""Workload-risk surface tests (Issue #149).

Covers:
  - GET /api/v1/health-workload/injury-risk role matrix: player-level rows for
    sportsperformance/admin, position-group aggregates for analyst, and
    403 policy_denied for coach/player/viewer.
  - Non-diagnostic disclaimer + caveat on every response.
  - POST /api/v1/health-workload/daily rejects player-attributed rows below
    the identity-confidence floor (defense in depth).
  - Audit events fire on reads and writes.
"""

from __future__ import annotations

import datetime
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

INJURY_RISK_URL = "/api/v1/health-workload/injury-risk"
DAILY_URL = "/api/v1/health-workload/daily"

PLAYER_LEVEL = [UserRole.admin, UserRole.sportsperformance]
AGGREGATE_ONLY = [UserRole.analyst]
DENIED = [UserRole.coach, UserRole.player, UserRole.viewer]


def _make_user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _override_db(all_rows: list[Any] | None = None, detail_row: Any = None) -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            result.all.return_value = all_rows or []
            result.scalar_one_or_none.return_value = detail_row
            scalars = MagicMock()
            scalars.all.return_value = all_rows or []
            result.scalars.return_value = scalars
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        session.add = MagicMock()
        yield session

    app.dependency_overrides[get_db] = _db


def _cleanup() -> None:
    app.dependency_overrides.clear()


def _workload_pair() -> tuple[Any, Any]:
    row = MagicMock()
    row.player_id = uuid.uuid4()
    row.date = datetime.date(2026, 7, 1)
    row.daily_load = 480.0
    row.sprint_count = 6
    row.max_speed_yps = 8.1
    row.asymmetry_index = 1.35
    row.acute_load_7d = 500.0
    row.chronic_load_28d = 300.0
    row.acwr = 1.67
    row.injury_risk_score = 0.55
    row.risk_reason_codes = ["acwr_above_threshold", "asymmetry_above_threshold"]
    row.clip_count = 12
    row.attribution = "player"
    row.confidence = 0.85
    row.model_variant = "acwr-asym-heuristic-v1"

    player = MagicMock()
    player.first_name = "Marcus"
    player.last_name = "Carter"
    player.jersey_number = 11
    player.position_group = "WR"
    return row, player


# ── Role matrix ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", PLAYER_LEVEL)
def test_injury_risk_player_level_for_sports_performance(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db(all_rows=[_workload_pair()])
    try:
        with TestClient(app) as c:
            resp = c.get(INJURY_RISK_URL)
    finally:
        _cleanup()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_level"] is True
    assert "rows" in body and "aggregates" not in body
    assert body["rows"][0]["name"] == "Marcus Carter"
    assert body["rows"][0]["latest"]["acwr"] == 1.67
    assert body["rows"][0]["caveat"]


@pytest.mark.parametrize("role", AGGREGATE_ONLY)
def test_injury_risk_aggregates_only_for_analyst(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db(all_rows=[_workload_pair()])
    try:
        with TestClient(app) as c:
            resp = c.get(INJURY_RISK_URL)
    finally:
        _cleanup()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_level"] is False
    assert "rows" not in body and "aggregates" in body
    agg = body["aggregates"][0]
    assert agg["position_group"] == "WR"
    assert agg["player_count"] == 1
    # No named players, no player ids in the aggregate view.
    assert "player_id" not in agg and "name" not in agg


@pytest.mark.parametrize("role", DENIED)
def test_injury_risk_denied_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.get(INJURY_RISK_URL)
    finally:
        _cleanup()
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"


def test_injury_risk_response_is_non_diagnostic() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db(all_rows=[_workload_pair()])
    try:
        with TestClient(app) as c:
            resp = c.get(INJURY_RISK_URL)
    finally:
        _cleanup()
    body = resp.json()
    assert "not a diagnosis" in body["caveat"].lower()
    assert "not a medical device" in body["disclaimer"].lower()


def test_injury_risk_emits_audit_event() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db(all_rows=[_workload_pair()])
    try:
        with (
            patch("app.routers.health_workload.audit_event") as audit,
            TestClient(app) as c,
        ):
            resp = c.get(INJURY_RISK_URL)
    finally:
        _cleanup()
    assert resp.status_code == 200
    events = [call.args[0] for call in audit.call_args_list]
    assert "audit.health_workload.injury_risk.read" in events
    kwargs = audit.call_args_list[events.index("audit.health_workload.injury_risk.read")].kwargs
    assert kwargs["surface"] == "injury_risk"
    # Identifiers/enums only — never metric values.
    assert "acwr" not in kwargs and "injury_risk_score" not in kwargs


# ── daily-loads aggregation ───────────────────────────────────────────────────


def _workload_metric(
    player_id: str,
    *,
    distance: float,
    identity_confidence: float | None = 0.9,
    asymmetry: float | None = None,
) -> Any:
    m = MagicMock()
    m.metric_name = "workload_fusion"
    m.metric_value = {
        "attribution": "player",
        "player_id": player_id,
        "position_group": "WR",
        "distance_yards": distance,
        "sprint_count": 2,
        "max_speed_yps": 7.0,
        "identity_confidence": identity_confidence,
        "confidence": 0.6,
    }
    m.sprint_count = 2
    m.asymmetry_index = asymmetry
    m.confidence = 0.6
    return m


def test_daily_loads_confidence_present_without_asymmetry() -> None:
    # Regression (PR #239 review): a player-day with load but NO pose
    # asymmetry must still carry identity-based confidence, or the rollup's
    # ingest gate would wrongly reject the row.
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.admin)
    _override_db(all_rows=[_workload_metric("p1", distance=120.0, asymmetry=None)])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/health-workload/daily-loads", params={"date": "2026-07-01"})
    finally:
        _cleanup()
    assert resp.status_code == 200, resp.text
    player = resp.json()["players"][0]
    assert player["asymmetry_index"] is None
    # Identity confidence (0.9) wins over generic clip confidence (0.6).
    assert player["confidence"] == 0.9
    assert player["daily_load"] == 120.0


# ── Upsert identity gating ────────────────────────────────────────────────────


def _daily_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "player_id": str(uuid.uuid4()),
        "date": "2026-07-01",
        "daily_load": 480.0,
        "sprint_count": 6,
        "acwr": 1.2,
        "injury_risk_score": 0.2,
        "attribution": "player",
        "confidence": 0.85,
        "model_variant": "acwr-asym-heuristic-v1",
    }
    row.update(overrides)
    return row


def test_daily_upsert_rejects_low_identity_confidence_player_rows() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.admin)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.post(DAILY_URL, json={"rows": [_daily_row(confidence=0.4)]})
    finally:
        _cleanup()
    assert resp.status_code == 422
    assert resp.json()["detail"]["error_code"] == "low_identity_confidence"


def test_daily_upsert_rejects_player_rows_without_confidence() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.admin)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.post(DAILY_URL, json={"rows": [_daily_row(confidence=None)]})
    finally:
        _cleanup()
    assert resp.status_code == 422


def test_daily_upsert_accepts_confident_player_rows() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.admin)
    _override_db(detail_row=None)  # no existing row → insert path
    try:
        with (
            patch("app.routers.health_workload.audit_event") as audit,
            TestClient(app) as c,
        ):
            resp = c.post(DAILY_URL, json={"rows": [_daily_row()]})
    finally:
        _cleanup()
    assert resp.status_code == 201, resp.text
    assert resp.json()["upserted"] == 1
    events = [call.args[0] for call in audit.call_args_list]
    assert "audit.health_workload.daily.write" in events


def test_daily_upsert_blocked_for_sportsperformance_write_path() -> None:
    # The worker write path is analyst-or-above; sportsperformance reads only.
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.post(DAILY_URL, json={"rows": [_daily_row()]})
    finally:
        _cleanup()
    assert resp.status_code == 403
