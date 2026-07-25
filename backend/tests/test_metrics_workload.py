"""Metric schema + access coverage for workload-fusion fields (Issue #149)."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Metric, User, UserRole
from app.routers.metrics import (
    HEALTH_WORKLOAD_ONLY_METRIC_NAMES,
    REVIEW_CANDIDATE_METRIC_NAMES,
    MetricCreate,
    MetricResponse,
)
from fastapi.testclient import TestClient


def test_metric_create_accepts_workload_fields() -> None:
    payload = MetricCreate(
        clip_id=uuid.uuid4(),
        tracklet_id=uuid.uuid4(),
        metric_name="workload_fusion",
        metric_value={"attribution": "player"},
        sprint_count=4,
        asymmetry_index=1.28,
        injury_risk_score=0.45,
    )

    assert payload.sprint_count == 4
    assert payload.asymmetry_index == 1.28
    assert payload.injury_risk_score == 0.45


def test_workload_fusion_is_always_experimental() -> None:
    # The create endpoint forces experimental_flag for review-candidate names —
    # workload_fusion must be in that set so risk signals can never be
    # pre-marked analytics_safe by a caller.
    assert "workload_fusion" in REVIEW_CANDIDATE_METRIC_NAMES


def test_metric_response_serializes_workload_fields() -> None:
    metric = MagicMock(spec=Metric)
    metric.id = uuid.uuid4()
    metric.clip_id = uuid.uuid4()
    metric.tracklet_id = uuid.uuid4()
    metric.metric_name = "workload_fusion"
    metric.metric_value = {"attribution": "player"}
    metric.unit = "review"
    metric.is_suppressed = False
    metric.suppression_reason = None
    metric.experimental_flag = True
    metric.analytics_safe = False
    metric.confidence = 0.82
    metric.effort_zscore = None
    metric.loaf_flag = None
    metric.sprint_count = 4
    metric.asymmetry_index = 1.28
    metric.injury_risk_score = 0.45
    metric.evidence_uri = None
    metric.model_version_id = None
    metric.calibration_version_id = None
    metric.job_id = None
    metric.created_at = datetime(2026, 7, 1, tzinfo=UTC)

    response = MetricResponse.from_orm_metric(metric)

    assert response.sprint_count == 4
    assert response.asymmetry_index == 1.28
    assert response.injury_risk_score == 0.45


# ── Generic metrics surface must not leak workload risk (PR #239 review) ──────


def _make_user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _override_db(scalar_result: Any = None, all_rows: list[Any] | None = None) -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = scalar_result
        result.scalars.return_value.all.return_value = all_rows or []
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[get_db] = _db


def test_workload_fusion_is_health_workload_only_metric() -> None:
    assert "workload_fusion" in HEALTH_WORKLOAD_ONLY_METRIC_NAMES


@pytest.mark.parametrize(
    "role",
    [UserRole.coach, UserRole.analyst, UserRole.viewer, UserRole.sportsperformance],
)
def test_get_workload_fusion_metric_hidden_from_generic_surface(role: UserRole) -> None:
    # workload_fusion is accessible ONLY via /api/v1/health-workload/* (full
    # RBAC + audit); the generic metrics surface 404s it for every role so the
    # health gate can never be bypassed and existence never leaks.
    metric = MagicMock(spec=Metric)
    metric.metric_name = "workload_fusion"
    metric.experimental_flag = True
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db(scalar_result=metric)
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/metrics/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_list_metrics_excludes_workload_fusion_for_all_roles() -> None:
    # The list query filters health-workload-only names out unconditionally;
    # an explicit metric_name filter simply returns an empty list.
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    _override_db(all_rows=[])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/metrics", params={"metric_name": "workload_fusion"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == []
