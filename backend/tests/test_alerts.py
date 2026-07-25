"""Tests for the Alert REST router (Issue #16).

Covers:
  - AlertCreate schema validation
  - AlertResponse.from_orm serialization
  - POST /api/v1/alerts — creation, player-role blocked
  - GET  /api/v1/alerts — list, position-group filtering, player blocked
  - GET  /api/v1/alerts/{id} — get single, player blocked
  - PATCH /api/v1/alerts/{id}/ack — acknowledgment, role enforcement
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.main import app
from app.models import Alert, AlertSeverity, AlertType, User, UserRole
from app.position_filter import position_group_filter
from app.routers.alerts import AlertCreate, AlertResponse
from fastapi import HTTPException
from fastapi.testclient import TestClient

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(role: UserRole, position_group: str | None = None) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    u.position_group = position_group
    return u


def _make_alert(
    position_group: str = "OL",
    alert_type: AlertType = AlertType.effort_anomaly,
    severity: AlertSeverity = AlertSeverity.medium,
) -> Alert:
    a = MagicMock(spec=Alert)
    a.id = uuid.uuid4()
    a.player_id = uuid.uuid4()
    a.clip_id = uuid.uuid4()
    a.position_group = position_group
    a.alert_type = alert_type
    a.severity = severity
    a.confidence = 0.75
    a.metric_name = "sprint_to_ball"
    a.metric_value = {"max_speed_yps": 4.2, "confidence": 0.75}
    a.deviation_sd = 2.3
    a.clip_uri = "r2://clips/test.mp4"
    a.period_name = "Period 2"
    a.session_id = "2026-05-08-practice"
    a.job_id = None
    a.is_acknowledged = False
    a.acknowledged_by = None
    a.acknowledged_at = None
    a.is_actioned = False
    a.actioned_by = None
    a.actioned_at = None
    a.created_at = datetime(2026, 5, 8, tzinfo=UTC)
    return a


def _mock_db(scalar_result: Any = None, all_result: list[Any] | None = None):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = scalar_result
        execute_result.scalars.return_value.all.return_value = all_result or []
        session.execute.return_value = execute_result
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        yield session

    return _db


# ── Schema unit tests ─────────────────────────────────────────────────────────


def test_alert_create_valid() -> None:
    body = AlertCreate(
        position_group="OL",
        alert_type=AlertType.effort_anomaly,
        severity=AlertSeverity.medium,
        confidence=0.8,
        metric_name="sprint_to_ball",
        metric_value={"max_speed_yps": 4.2},
    )
    assert body.position_group == "OL"
    assert body.confidence == 0.8


def test_alert_create_confidence_out_of_range_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AlertCreate(
            position_group="OL",
            alert_type=AlertType.effort_anomaly,
            severity=AlertSeverity.medium,
            confidence=1.5,
            metric_name="sprint_to_ball",
            metric_value={},
        )


def test_alert_response_from_orm() -> None:
    alert = _make_alert()
    resp = AlertResponse.from_orm(alert)
    assert resp.position_group == "OL"
    assert resp.alert_type == "effort_anomaly"
    assert resp.severity == "medium"
    assert resp.is_acknowledged is False
    assert resp.created_at == "2026-05-08T00:00:00+00:00"


def test_alert_response_from_orm_acknowledged() -> None:
    alert = _make_alert()
    alert.is_acknowledged = True
    alert.acknowledged_by = uuid.uuid4()
    alert.acknowledged_at = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    resp = AlertResponse.from_orm(alert)
    assert resp.is_acknowledged is True
    assert resp.acknowledged_at == "2026-05-08T12:00:00+00:00"


# ── Endpoint tests ────────────────────────────────────────────────────────────


def test_create_alert_player_role_blocked() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    player = _make_user(UserRole.player)
    app.dependency_overrides[get_current_user] = lambda: player
    app.dependency_overrides[get_db] = _mock_db()

    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/alerts",
            json={
                "position_group": "OL",
                "alert_type": "effort_anomaly",
                "severity": "medium",
                "confidence": 0.7,
                "metric_name": "sprint_to_ball",
                "metric_value": {},
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_create_alert_coach_allowed() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    mock_alert = _make_alert()
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(scalar_result=mock_alert)

    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/alerts",
            json={
                "position_group": "OL",
                "alert_type": "effort_anomaly",
                "severity": "medium",
                "confidence": 0.7,
                "metric_name": "sprint_to_ball",
                "metric_value": {"max_speed_yps": 3.1},
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 201


def test_create_alert_publishes_to_sse() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db()

    with (
        patch("app.routers.alerts.publish_alert") as mock_publish,
        TestClient(app) as c,
    ):
        resp = c.post(
            "/api/v1/alerts",
            json={
                "position_group": "OL",
                "alert_type": "effort_anomaly",
                "severity": "medium",
                "confidence": 0.7,
                "metric_name": "sprint_to_ball",
                "metric_value": {"max_speed_yps": 3.1},
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 201
    mock_publish.assert_called_once()


def test_list_alerts_player_blocked() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    player = _make_user(UserRole.player)
    app.dependency_overrides[get_current_user] = lambda: player
    app.dependency_overrides[get_db] = _mock_db()

    with TestClient(app) as c:
        resp = c.get("/api/v1/alerts")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_list_alerts_coach_sees_own_group() -> None:
    from app.database import get_db
    from app.deps import get_current_user
    from app.position_filter import position_group_filter

    coach = _make_user(UserRole.coach, "OL")
    ol_alert = _make_alert(position_group="OL")
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(all_result=[ol_alert])
    app.dependency_overrides[position_group_filter] = lambda: "OL"

    with TestClient(app) as c:
        resp = c.get("/api/v1/alerts")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["position_group"] == "OL"


def test_list_alerts_analyst_sees_all_groups() -> None:
    from app.database import get_db
    from app.deps import get_current_user
    from app.position_filter import position_group_filter

    analyst = _make_user(UserRole.analyst)
    alerts = [_make_alert("OL"), _make_alert("WR"), _make_alert("QB")]
    app.dependency_overrides[get_current_user] = lambda: analyst
    app.dependency_overrides[get_db] = _mock_db(all_result=alerts)
    app.dependency_overrides[position_group_filter] = lambda: None

    with TestClient(app) as c:
        resp = c.get("/api/v1/alerts")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_get_alert_not_found() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(scalar_result=None)

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/alerts/{uuid.uuid4()}")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


# ── Restricted workload-risk alerts (Issue #149 / #9) ────────────────────────


@pytest.mark.parametrize("role", [UserRole.coach, UserRole.analyst])
def test_list_alerts_explicit_workload_risk_filter_blocked(role: UserRole) -> None:
    from app.database import get_db
    from app.deps import get_current_user
    from app.position_filter import position_group_filter

    user = _make_user(role, "OL" if role is UserRole.coach else None)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _mock_db(all_result=[])
    app.dependency_overrides[position_group_filter] = lambda: None

    with TestClient(app) as c:
        resp = c.get("/api/v1/alerts", params={"alert_type": "workload_risk"})
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_list_alerts_workload_risk_filter_allowed_for_sportsperformance() -> None:
    from app.database import get_db
    from app.deps import get_current_user
    from app.position_filter import position_group_filter

    sp = _make_user(UserRole.sportsperformance, "OL")
    wr_alert = _make_alert(position_group="OL", alert_type=AlertType.workload_risk)
    wr_alert.metric_name = "workload_fusion"
    app.dependency_overrides[get_current_user] = lambda: sp
    app.dependency_overrides[get_db] = _mock_db(all_result=[wr_alert])
    app.dependency_overrides[position_group_filter] = lambda: None

    with TestClient(app) as c:
        resp = c.get("/api/v1/alerts", params={"alert_type": "workload_risk"})
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()[0]["alert_type"] == "workload_risk"


@pytest.mark.parametrize("role", [UserRole.coach, UserRole.analyst])
def test_get_workload_risk_alert_hidden_as_404(role: UserRole) -> None:
    from app.database import get_db
    from app.deps import get_current_user

    user = _make_user(role, "OL" if role is UserRole.coach else None)
    wr_alert = _make_alert(position_group="OL", alert_type=AlertType.workload_risk)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _mock_db(scalar_result=wr_alert)

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/alerts/{wr_alert.id}")
    app.dependency_overrides.clear()
    # 404, not 403 — the alert's existence must not leak to excluded roles.
    assert resp.status_code == 404


def test_get_workload_risk_alert_visible_to_sportsperformance() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    sp = _make_user(UserRole.sportsperformance, "OL")
    wr_alert = _make_alert(position_group="OL", alert_type=AlertType.workload_risk)
    app.dependency_overrides[get_current_user] = lambda: sp
    app.dependency_overrides[get_db] = _mock_db(scalar_result=wr_alert)

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/alerts/{wr_alert.id}")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["alert_type"] == "workload_risk"


def test_ack_workload_risk_alert_hidden_from_coach() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    wr_alert = _make_alert(position_group="OL", alert_type=AlertType.workload_risk)
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(scalar_result=wr_alert)

    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/alerts/{wr_alert.id}/ack")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_acknowledge_alert_coach_allowed() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    mock_alert = _make_alert()
    mock_alert.is_acknowledged = False
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(scalar_result=mock_alert)

    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/alerts/{mock_alert.id}/ack")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert mock_alert.is_acknowledged is True
    assert mock_alert.acknowledged_by == coach.id


def test_acknowledge_alert_viewer_blocked(client: TestClient) -> None:
    resp = client.patch(f"/api/v1/alerts/{uuid.uuid4()}/ack")
    assert resp.status_code in (401, 403)


def test_action_alert_coach_allowed() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    mock_alert = _make_alert(alert_type=AlertType.formation_tendency)
    mock_alert.is_actioned = False
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(scalar_result=mock_alert)

    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/alerts/{mock_alert.id}/action")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert mock_alert.is_actioned is True
    assert mock_alert.actioned_by == coach.id
    assert resp.json()["is_actioned"] is True


def test_action_alert_not_found() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach, "OL")
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db(scalar_result=None)

    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/alerts/{uuid.uuid4()}/action")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_action_alert_viewer_blocked(client: TestClient) -> None:
    resp = client.patch(f"/api/v1/alerts/{uuid.uuid4()}/action")
    assert resp.status_code in (401, 403)


def test_alert_response_from_orm_actioned() -> None:
    alert = _make_alert(alert_type=AlertType.formation_tendency)
    alert.is_actioned = True
    alert.actioned_by = uuid.uuid4()
    alert.actioned_at = datetime(2026, 5, 9, 9, 30, 0, tzinfo=UTC)
    resp = AlertResponse.from_orm(alert)
    assert resp.alert_type == "formation_tendency"
    assert resp.is_actioned is True
    assert resp.actioned_at == "2026-05-09T09:30:00+00:00"


# ── Position-group filter unit tests ─────────────────────────────────────────


def test_alert_response_carries_required_fields() -> None:
    alert = _make_alert()
    resp = AlertResponse.from_orm(alert)
    assert resp.confidence is not None
    assert resp.clip_uri is not None
    assert resp.player_id is not None
    assert resp.created_at is not None
    assert resp.position_group is not None


@pytest.mark.asyncio
async def test_position_group_filter_blocks_unassigned_coach() -> None:
    coach = _make_user(UserRole.coach, None)
    with pytest.raises(HTTPException) as exc:
        await position_group_filter(current_user=coach, position_group=None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_position_group_filter_coach_cannot_override_cross_group() -> None:
    coach = _make_user(UserRole.coach, "WR")
    effective_pg = await position_group_filter(current_user=coach, position_group="OL")
    assert effective_pg == "WR"
