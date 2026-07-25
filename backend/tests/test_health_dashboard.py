"""Daily athlete-state dashboard tests (Issue #9).

Covers the pure fusion shaping (tier gates, player-level vs aggregates,
source caveats), the restricted-context tier helpers (escalation-only), the
endpoint role matrix, fatigue-flag visibility, and audit events.
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
from app.governance import allowed_context_tiers, can_view_field, effective_field_tier
from app.main import app
from app.models import User, UserRole
from app.workload_fusion import DailyStateInputs, fuse_daily_athlete_state
from fastapi.testclient import TestClient

DASHBOARD_URL = "/api/v1/health-workload/dashboard"
AS_OF = datetime.date(2026, 7, 1)


# ── Restricted-context tier helpers ───────────────────────────────────────────


def test_field_tiers_default_mapping() -> None:
    assert effective_field_tier("acwr") == "workload"
    assert effective_field_tier("injury_history") == "rehab"
    assert effective_field_tier("medical") == "medical"


def test_flags_can_escalate_but_never_widen() -> None:
    # Escalation: workload → rehab honored.
    assert effective_field_tier("acwr", {"acwr": "rehab"}) == "rehab"
    # Attempted de-escalation: rehab → workload is IGNORED.
    assert effective_field_tier("injury_history", {"injury_history": "workload"}) == "rehab"
    # Unknown tier names are ignored.
    assert effective_field_tier("acwr", {"acwr": "public"}) == "workload"


def test_tier_role_matrix() -> None:
    assert allowed_context_tiers(UserRole.sportsperformance) == {"workload", "rehab"}
    assert allowed_context_tiers(UserRole.analyst) == {"workload"}
    assert allowed_context_tiers(UserRole.coach) == frozenset()
    # The medical tier is declared but mapped to NO role.
    assert not can_view_field(UserRole.admin, "medical")


def test_rehab_tier_blocked_for_analyst() -> None:
    assert can_view_field(UserRole.sportsperformance, "injury_history") is True
    assert can_view_field(UserRole.analyst, "injury_history") is False


# ── Pure fusion shaping ───────────────────────────────────────────────────────


def _workload_pair(player_id: uuid.UUID | None = None) -> tuple[Any, Any]:
    row = MagicMock()
    row.player_id = player_id or uuid.uuid4()
    row.date = AS_OF
    row.daily_load = 480.0
    row.sprint_count = 6
    row.max_speed_yps = 8.1
    row.asymmetry_index = 1.2
    row.acwr = 1.1
    row.injury_risk_score = 0.15
    row.risk_reason_codes = []
    row.confidence = 0.85
    row.model_variant = "acwr-asym-heuristic-v1"

    player = MagicMock()
    player.first_name = "Marcus"
    player.last_name = "Carter"
    player.jersey_number = 11
    player.position_group = "WR"
    return row, player


def _wellness(player_id: uuid.UUID) -> Any:
    w = MagicMock()
    w.player_id = player_id
    w.soreness = 4
    w.sleep_hours = 7.5
    w.sleep_quality = 6
    w.energy = 7
    w.stress = 3
    return w


def _injury(player_id: uuid.UUID) -> Any:
    i = MagicMock()
    i.player_id = player_id
    i.reported_date = datetime.date(2026, 6, 20)
    i.body_region = "hamstring"
    i.side = "L"
    i.status = "active"
    i.days_missed = 4
    return i


def test_fusion_player_level_for_sportsperformance() -> None:
    row, player = _workload_pair()
    inputs = DailyStateInputs(
        date=AS_OF,
        workload_rows=[(row, player)],
        wellness_rows=[_wellness(row.player_id)],
        injury_rows=[_injury(row.player_id)],
        source_counts={"wellness": 1, "injury_history": 1},
    )

    payload = fuse_daily_athlete_state(inputs, role=UserRole.sportsperformance)

    assert payload["player_level"] is True
    assert "medical diagnosis" in payload["policy_statement"]
    state = payload["players"][0]
    assert state["cv_workload"]["acwr"] == 1.1
    assert state["cv_workload"]["caveat"]
    assert state["wellness"]["soreness"] == 4
    assert state["wellness"]["caveat"].startswith("Self-reported")
    # Rehab tier visible to sports performance.
    assert state["injury_history"][0]["body_region"] == "hamstring"
    # Missing sources are explicit caveats, never silent.
    assert "gps:source_unavailable" in state["caveats"]
    assert "strength_conditioning:source_unavailable" in state["caveats"]


def test_fusion_aggregates_only_for_analyst() -> None:
    inputs = DailyStateInputs(date=AS_OF, workload_rows=[_workload_pair()])

    payload = fuse_daily_athlete_state(inputs, role=UserRole.analyst)

    assert payload["player_level"] is False
    assert "players" not in payload and "aggregates" in payload
    agg = payload["aggregates"][0]
    assert agg["position_group"] == "WR"
    assert "name" not in agg and "player_id" not in agg


def test_fusion_honors_per_player_tier_escalation() -> None:
    row, player = _workload_pair()
    inputs = DailyStateInputs(
        date=AS_OF,
        workload_rows=[(row, player)],
        # This player's CV workload has been escalated to the medical tier —
        # nobody may see it through the API, not even sports performance.
        profile_flags={str(row.player_id): {"acwr": "medical"}},
    )

    payload = fuse_daily_athlete_state(inputs, role=UserRole.sportsperformance)

    state = payload["players"][0]
    assert state["cv_workload"] is None
    assert "cv_workload:tier_restricted" in state["caveats"]


# ── Endpoint role matrix ──────────────────────────────────────────────────────


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
        result.all.return_value = []
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[get_db] = _db


def _cleanup() -> None:
    app.dependency_overrides.clear()


def _empty_inputs(*_args: Any, **_kwargs: Any) -> DailyStateInputs:
    return DailyStateInputs(date=AS_OF)


@pytest.mark.parametrize("role", [UserRole.admin, UserRole.sportsperformance])
def test_dashboard_player_level_roles_get_players_and_flags(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db()
    try:
        with (
            patch(
                "app.routers.health_workload.fetch_daily_state_inputs",
                new=AsyncMock(side_effect=lambda *a, **k: _empty_inputs()),
            ),
            TestClient(app) as c,
        ):
            resp = c.get(DASHBOARD_URL)
    finally:
        _cleanup()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_level"] is True
    assert "players" in body
    assert "fatigue_flags" in body  # S&C staff see fatigue flags
    assert "policy_statement" in body
    assert "trends" in body and "integrations" in body


def test_dashboard_analyst_gets_aggregates_without_fatigue_flags() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.analyst)
    _override_db()
    try:
        with (
            patch(
                "app.routers.health_workload.fetch_daily_state_inputs",
                new=AsyncMock(side_effect=lambda *a, **k: _empty_inputs()),
            ),
            TestClient(app) as c,
        ):
            resp = c.get(DASHBOARD_URL)
    finally:
        _cleanup()
    assert resp.status_code == 200
    body = resp.json()
    assert body["player_level"] is False
    assert "aggregates" in body and "players" not in body
    # Fatigue flags are S&C-only — analysts never receive them.
    assert "fatigue_flags" not in body


@pytest.mark.parametrize("role", [UserRole.coach, UserRole.player, UserRole.viewer])
def test_dashboard_denied_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db()
    try:
        with TestClient(app) as c:
            resp = c.get(DASHBOARD_URL)
    finally:
        _cleanup()
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"


def test_dashboard_emits_audit_event_without_health_values() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db()
    try:
        with (
            patch(
                "app.routers.health_workload.fetch_daily_state_inputs",
                new=AsyncMock(side_effect=lambda *a, **k: _empty_inputs()),
            ),
            patch("app.routers.health_workload.audit_event") as audit,
            TestClient(app) as c,
        ):
            resp = c.get(DASHBOARD_URL)
    finally:
        _cleanup()
    assert resp.status_code == 200
    events = [call.args[0] for call in audit.call_args_list]
    assert "audit.health_workload.dashboard.read" in events
    kwargs = audit.call_args_list[events.index("audit.health_workload.dashboard.read")].kwargs
    assert kwargs["surface"] == "dashboard"
    for forbidden in ("acwr", "asymmetry_index", "injury_risk_score", "soreness"):
        assert forbidden not in kwargs
