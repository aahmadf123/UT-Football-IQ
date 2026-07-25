"""Tests for the Pose-Lite Biomechanics router (Issue #6).

Tests cover:
  - Schema validation for PoseKeypointsCreate / PoseMetricSummary
  - POSE_METRIC_NAMES set membership
  - _position_group_metric_names helper
  - Experimental-flag enforcement (pose metrics are always experimental)
  - Role-based access: player role blocked, coach+ allowed
  - Pending-metrics endpoint returns experimental metrics
  - Keypoints list endpoint filtering
  - Coach approve action sets analytics_safe=True
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.main import app
from app.models import HeadOrientationReview, Metric, PoseKeypoints, User, UserRole
from app.routers.pose import (
    POSE_METRIC_NAMES,
    PoseKeypointsCreate,
    PoseKeypointsResponse,
    PoseMetricSummary,
    ReviewCreate,
    _position_group_metric_names,
)
from fastapi.testclient import TestClient

# ── Pure-function / schema unit tests ─────────────────────────────────────────


def test_pose_metric_names_all_prefixed() -> None:
    for name in POSE_METRIC_NAMES:
        assert name.startswith("pose_"), f"{name!r} does not start with 'pose_'"


def test_pose_metric_names_count() -> None:
    assert len(POSE_METRIC_NAMES) == 15


def test_position_group_metrics_ol_contains_pad_level() -> None:
    names = _position_group_metric_names("OL")
    assert "pose_pad_level" in names
    assert "pose_block_shed_timing" in names
    assert "pose_pass_set_weight_distribution" in names


def test_position_group_metrics_wr_contains_breakpoint() -> None:
    names = _position_group_metric_names("WR")
    assert "pose_wr_shoulder_over_knee" in names
    assert "pose_wr_deceleration_steps" in names
    assert "pose_wr_release_technique" in names


def test_position_group_metrics_qb_contains_mechanics() -> None:
    names = _position_group_metric_names("QB")
    assert "pose_qb_stride_consistency" in names
    assert "pose_qb_shoulder_hip_separation" in names


def test_position_group_metrics_db_contains_body_orientation_proxy() -> None:
    names = _position_group_metric_names("DB")
    assert "pose_body_orientation_proxy" in names


def test_position_group_metrics_all_returns_all_pose_names() -> None:
    assert _position_group_metric_names("all") == POSE_METRIC_NAMES


def test_position_group_metrics_unknown_group_returns_all() -> None:
    assert _position_group_metric_names("UNKNOWN") == POSE_METRIC_NAMES


# ── Pydantic schema validation ─────────────────────────────────────────────────


def test_pose_keypoints_create_valid() -> None:
    tid = uuid.uuid4()
    data = PoseKeypointsCreate(
        tracklet_id=tid,
        frame_number=42,
        keypoints=[{"name": "nose", "x": 640.0, "y": 100.0, "confidence": 0.9}],
    )
    assert data.frame_number == 42
    assert data.tracklet_id == tid


def test_pose_keypoints_create_missing_tracklet_id_rejected() -> None:
    import pydantic

    # tracklet_id is required because the underlying DB column is NOT NULL.
    with pytest.raises(pydantic.ValidationError):
        PoseKeypointsCreate(
            frame_number=0,
            keypoints=[{"name": "nose", "x": 1.0, "y": 1.0, "confidence": 0.9}],
        )


def test_pose_keypoints_create_negative_frame_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PoseKeypointsCreate(tracklet_id=uuid.uuid4(), frame_number=-1, keypoints=[])


def test_pose_keypoints_create_invalid_confidence_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PoseKeypointsCreate(
            tracklet_id=uuid.uuid4(),
            frame_number=0,
            keypoints=[],
            head_orientation_confidence=1.5,  # > 1.0
        )


def test_review_create_valid_actions() -> None:
    for action in ("approve", "reject", "flag"):
        r = ReviewCreate(review_action=action)
        assert r.review_action == action


def test_review_create_invalid_action_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ReviewCreate(review_action="delete")


def test_pose_keypoints_response_from_orm() -> None:
    pk = MagicMock(spec=PoseKeypoints)
    pk.id = uuid.uuid4()
    pk.tracklet_id = uuid.uuid4()
    pk.frame_number = 10
    pk.keypoints = [{"name": "nose", "x": 1.0, "y": 2.0, "confidence": 0.8}]
    pk.head_yaw_degrees = 15.0
    pk.head_orientation_confidence = 0.7
    pk.biomechanics = {"hip_flexion_degrees": 45.0}
    pk.model_version_id = None
    pk.job_id = None
    pk.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    resp = PoseKeypointsResponse.from_orm(pk)
    assert resp.frame_number == 10
    assert resp.biomechanics == {"hip_flexion_degrees": 45.0}
    assert resp.created_at == "2026-01-01T00:00:00+00:00"


def test_pose_metric_summary_from_orm() -> None:
    m = MagicMock(spec=Metric)
    m.id = uuid.uuid4()
    m.clip_id = uuid.uuid4()
    m.tracklet_id = None
    m.metric_name = "pose_pad_level"
    m.metric_value = {"hip_flexion_degrees": 45.0, "confidence": 0.88}
    m.confidence = 0.88
    m.experimental_flag = True
    m.analytics_safe = False
    m.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    summary = PoseMetricSummary.from_orm(m)
    assert summary.metric_name == "pose_pad_level"
    assert summary.experimental_flag is True
    assert summary.analytics_safe is False


# ── Endpoint tests with mocked DB + auth ──────────────────────────────────────


def _make_user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _mock_db_override(scalar_result: Any = None, all_result: list[Any] | None = None):
    """Return an async DB override that yields a mock session."""

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = scalar_result
        execute_result.scalars.return_value.all.return_value = all_result or []
        session.execute.return_value = execute_result
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        yield session

    return _db


def test_pose_keypoints_list_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/pose/keypoints")
    assert resp.status_code in (401, 403)


def test_pose_pending_metrics_requires_coach_role() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    player = _make_user(UserRole.player)

    app.dependency_overrides[get_current_user] = lambda: player
    app.dependency_overrides[get_db] = _mock_db_override(all_result=[])

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/metrics/pending")
    app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_pose_keypoints_list_player_forbidden() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    player = _make_user(UserRole.player)

    app.dependency_overrides[get_current_user] = lambda: player
    app.dependency_overrides[get_db] = _mock_db_override()

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/keypoints")
    app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_pose_keypoints_list_viewer_forbidden() -> None:
    """Viewers must not see raw pose keypoints — they are an internal
    coaching artifact. Regression test for Devin Review flag pose.py:282-286.
    """
    from app.database import get_db
    from app.deps import get_current_user

    viewer = _make_user(UserRole.viewer)

    app.dependency_overrides[get_current_user] = lambda: viewer
    app.dependency_overrides[get_db] = _mock_db_override()

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/keypoints")
    app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_pose_keypoints_detail_viewer_forbidden() -> None:
    """Viewers must not see raw pose keypoints by ID either."""
    from app.database import get_db
    from app.deps import get_current_user

    viewer = _make_user(UserRole.viewer)

    app.dependency_overrides[get_current_user] = lambda: viewer
    app.dependency_overrides[get_db] = _mock_db_override()

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/pose/keypoints/{uuid.uuid4()}")
    app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_pose_keypoints_list_coach_allowed() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach)
    mock_kp = MagicMock(spec=PoseKeypoints)
    mock_kp.id = uuid.uuid4()
    mock_kp.tracklet_id = uuid.uuid4()
    mock_kp.frame_number = 5
    mock_kp.keypoints = [{"name": "nose", "x": 100.0, "y": 50.0, "confidence": 0.9}]
    mock_kp.head_yaw_degrees = None
    mock_kp.head_orientation_confidence = None
    mock_kp.biomechanics = None
    mock_kp.model_version_id = None
    mock_kp.job_id = None
    mock_kp.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db_override(all_result=[mock_kp])

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/keypoints")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["frame_number"] == 5


def test_pose_pending_metrics_coach_sees_experimental() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach)
    mock_metric = MagicMock(spec=Metric)
    mock_metric.id = uuid.uuid4()
    mock_metric.clip_id = uuid.uuid4()
    mock_metric.tracklet_id = None
    mock_metric.metric_name = "pose_pad_level"
    mock_metric.metric_value = {"hip_flexion_degrees": 32.0}
    mock_metric.confidence = 0.85
    mock_metric.experimental_flag = True
    mock_metric.analytics_safe = False
    mock_metric.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db_override(all_result=[mock_metric])

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/metrics/pending")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["metric_name"] == "pose_pad_level"
    assert data[0]["experimental_flag"] is True


def test_pose_asymmetry_restricted_to_sportsperformance() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach)

    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _mock_db_override()

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/asymmetry")
    app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_pose_asymmetry_sportsperformance_allowed() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    sp = _make_user(UserRole.sportsperformance)
    mock_metric = MagicMock(spec=Metric)
    mock_metric.id = uuid.uuid4()
    mock_metric.clip_id = uuid.uuid4()
    mock_metric.tracklet_id = uuid.uuid4()
    mock_metric.metric_value = {
        "left_right_ratio": 0.98,
        "asymmetry_score": 0.05,
        "injury_risk_flag": False,
    }
    mock_metric.confidence = 0.9
    mock_metric.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    app.dependency_overrides[get_current_user] = lambda: sp
    app.dependency_overrides[get_db] = _mock_db_override(all_result=[mock_metric])

    with TestClient(app) as c:
        resp = c.get("/api/v1/pose/asymmetry")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["asymmetry_score"] == pytest.approx(0.05)


def test_pose_review_coach_approve_sets_analytics_safe() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    coach = _make_user(UserRole.coach)
    metric_id = uuid.uuid4()
    mock_metric = MagicMock(spec=Metric)
    mock_metric.id = metric_id
    mock_metric.metric_name = "pose_pad_level"
    mock_metric.experimental_flag = True
    mock_metric.analytics_safe = False

    review = MagicMock(spec=HeadOrientationReview)
    review.id = uuid.uuid4()
    review.metric_id = metric_id
    review.reviewer_id = coach.id
    review.review_action = "approve"
    review.position_group = "OL"
    review.notes = None
    review.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = mock_metric
    session.execute.return_value = execute_result
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(
        side_effect=lambda obj: setattr(
            obj,
            "created_at",
            datetime(2026, 1, 1, tzinfo=UTC),
        )
    )

    async def _db():
        yield session

    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[get_db] = _db

    with TestClient(app) as c:
        resp = c.post(
            f"/api/v1/pose/metrics/{metric_id}/review",
            json={"review_action": "approve", "position_group": "OL"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 201
    assert mock_metric.analytics_safe is True
