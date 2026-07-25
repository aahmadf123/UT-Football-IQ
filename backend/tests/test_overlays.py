"""Tests for the clip-overlay aggregation endpoint (Issue #104).

The endpoint at ``GET /api/v1/clips/{clip_id}/overlays`` should return
tracklets (with inline track points), events, labels, and metrics for a
single clip in one structured payload. Behavior verified here:
  - 404 when the clip does not exist
  - empty layers when the clip exists but has no overlay data
  - layers_available reflects what is actually present
  - suppressed metrics are excluded from the overlay surface
  - player-role users do not see experimental metrics
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    Clip,
    Event,
    Label,
    Metric,
    Tracklet,
    TrackPoint,
    User,
    UserRole,
)
from fastapi.testclient import TestClient


def _make_user(role: UserRole = UserRole.coach) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _make_clip(**overrides: Any) -> Clip:
    c = MagicMock(spec=Clip)
    c.id = overrides.get("id", uuid.uuid4())
    c.video_id = overrides.get("video_id", uuid.uuid4())
    c.start_time = 0.0
    c.end_time = 5.0
    c.capture_regime = overrides.get("capture_regime", None)
    return c


def _make_tracklet(*, clip_id: uuid.UUID, points: list[TrackPoint] | None = None) -> Tracklet:
    t = MagicMock(spec=Tracklet)
    t.id = uuid.uuid4()
    t.clip_id = clip_id
    t.player_id = None
    t.start_frame = 0
    t.end_frame = 30
    t.track_confidence = 0.85
    t.team_label = "home"
    t.position_group = "Skill"
    t.side_of_ball = "offense"
    t.track_points = points or []
    t.created_at = datetime.now(UTC)
    return t


def _make_point(frame: int, x: float, y: float) -> TrackPoint:
    p = MagicMock(spec=TrackPoint)
    p.id = uuid.uuid4()
    p.frame_number = frame
    p.field_x = x
    p.field_y = y
    p.bbox = [10.0, 20.0, 30.0, 40.0]
    p.detection_confidence = 0.9
    return p


def _make_event(*, clip_id: uuid.UUID, event_type: str = "snap", frame: int = 10) -> Event:
    e = MagicMock(spec=Event)
    e.id = uuid.uuid4()
    e.clip_id = clip_id
    e.event_type = event_type
    e.frame_number = frame
    e.timestamp_seconds = float(frame) / 30.0
    e.attributes = None
    e.created_by = None
    e.model_version_id = None
    e.created_at = datetime.now(UTC)
    return e


def _make_label(*, clip_id: uuid.UUID, label_type: str = "offensive_formation") -> Label:
    lb = MagicMock(spec=Label)
    lb.id = uuid.uuid4()
    lb.clip_id = clip_id
    lb.tracklet_id = None
    lb.label_type = label_type
    lb.label_value = {"name": "trips_right"}
    lb.source = "model"
    lb.annotated_by = None
    lb.dataset_version = None
    lb.created_at = datetime.now(UTC)
    return lb


def _make_metric(
    *,
    clip_id: uuid.UUID,
    name: str = "yards_gained",
    is_suppressed: bool = False,
    experimental_flag: bool = False,
) -> Metric:
    m = MagicMock(spec=Metric)
    m.id = uuid.uuid4()
    m.clip_id = clip_id
    m.tracklet_id = None
    m.metric_name = name
    m.metric_value = {"value": 7}
    m.unit = "yards"
    m.is_suppressed = is_suppressed
    m.suppression_reason = None
    m.experimental_flag = experimental_flag
    m.analytics_safe = not experimental_flag
    m.confidence = 0.9
    m.evidence_uri = None
    m.model_version_id = None
    m.calibration_version_id = None
    m.job_id = None
    m.created_at = datetime.now(UTC)
    return m


def _override_auth(role: UserRole = UserRole.coach) -> User:
    user = _make_user(role)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _make_db(
    *,
    clip: Clip | None,
    tracklets: list[Tracklet],
    events: list[Event],
    labels: list[Label],
    metrics: list[Metric],
) -> Any:
    """Build an async-session stand-in that returns the right rows for each
    SELECT the endpoint issues, in order: clip, tracklets, events, labels,
    metrics. Filter predicates (e.g. experimental_flag) are exercised at the
    SQL level — the mock returns whatever caller passes for ``metrics`` and we
    assert that the endpoint filters the wrong-shape rows out via separate
    test fixtures."""

    calls = {"n": 0}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            calls["n"] += 1
            result = MagicMock()
            n = calls["n"]
            if n == 1:
                result.scalar_one_or_none.return_value = clip
            elif n == 2:
                # Latest FieldCalibration for the clip's video — none in these
                # fixtures, exercising the default "no_calibration" block.
                result.scalar_one_or_none.return_value = None
            elif n == 3:
                result.scalars.return_value.all.return_value = tracklets
            elif n == 4:
                result.scalars.return_value.all.return_value = events
            elif n == 5:
                result.scalars.return_value.all.return_value = labels
            else:
                result.scalars.return_value.all.return_value = metrics
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    return _db


# ── 404 ───────────────────────────────────────────────────────────────────────


def test_get_overlays_returns_404_when_clip_missing() -> None:
    _override_auth()
    app.dependency_overrides[get_db] = _make_db(
        clip=None, tracklets=[], events=[], labels=[], metrics=[]
    )
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{uuid.uuid4()}/overlays")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── Empty (clip exists but no data) ───────────────────────────────────────────


def test_get_overlays_returns_empty_layers_for_clip_without_data() -> None:
    clip = _make_clip()
    _override_auth()
    app.dependency_overrides[get_db] = _make_db(
        clip=clip, tracklets=[], events=[], labels=[], metrics=[]
    )
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{clip.id}/overlays")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["clip_id"] == str(clip.id)
    assert body["tracklets"] == []
    assert body["events"] == []
    assert body["labels"] == []
    assert body["metrics"] == []
    assert body["layers_available"] == {
        "tracklets": False,
        "events": False,
        "labels": False,
        "metrics": False,
    }


# ── Full payload ──────────────────────────────────────────────────────────────


def test_get_overlays_returns_full_payload_with_track_points() -> None:
    clip = _make_clip()
    points = [_make_point(0, 10.0, 20.0), _make_point(5, 12.0, 22.0)]
    tracklets = [_make_tracklet(clip_id=clip.id, points=points)]
    events = [_make_event(clip_id=clip.id, event_type="snap", frame=10)]
    labels = [_make_label(clip_id=clip.id)]
    metrics = [_make_metric(clip_id=clip.id, name="yards_gained")]

    _override_auth()
    app.dependency_overrides[get_db] = _make_db(
        clip=clip, tracklets=tracklets, events=events, labels=labels, metrics=metrics
    )
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{clip.id}/overlays")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layers_available"] == {
        "tracklets": True,
        "events": True,
        "labels": True,
        "metrics": True,
    }
    assert len(body["tracklets"]) == 1
    t = body["tracklets"][0]
    assert t["start_frame"] == 0
    assert t["team_label"] == "home"
    assert len(t["track_points"]) == 2
    assert t["track_points"][0]["frame_number"] == 0
    assert t["track_points"][0]["field_x"] == 10.0

    assert body["events"][0]["event_type"] == "snap"
    assert body["labels"][0]["label_type"] == "offensive_formation"
    assert body["metrics"][0]["metric_name"] == "yards_gained"


# ── Degraded (some layers missing) ────────────────────────────────────────────


def test_get_overlays_layers_available_reflects_partial_data() -> None:
    clip = _make_clip()
    tracklets = [_make_tracklet(clip_id=clip.id, points=[])]

    _override_auth()
    app.dependency_overrides[get_db] = _make_db(
        clip=clip, tracklets=tracklets, events=[], labels=[], metrics=[]
    )
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{clip.id}/overlays")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layers_available"]["tracklets"] is True
    assert body["layers_available"]["events"] is False
    assert body["layers_available"]["labels"] is False
    assert body["layers_available"]["metrics"] is False


# ── Filters (verified at SQL level) ───────────────────────────────────────────


def test_get_overlays_filters_out_suppressed_metrics_in_sql() -> None:
    clip = _make_clip()
    captured: dict[str, str] = {}
    calls = {"n": 0}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            calls["n"] += 1
            result = MagicMock()
            if calls["n"] == 1:
                result.scalar_one_or_none.return_value = clip
            elif calls["n"] == 2:
                result.scalar_one_or_none.return_value = None
            elif calls["n"] == 6:
                captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
                result.scalars.return_value.all.return_value = []
            else:
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth(role=UserRole.coach)
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{clip.id}/overlays")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    where_clause = captured["sql"].split("WHERE", 1)[1]
    assert "metrics.is_suppressed" in where_clause
    # Coach role: experimental_flag filter must NOT be applied.
    assert "experimental_flag" not in where_clause


def test_get_overlays_filters_out_experimental_metrics_for_player_role() -> None:
    clip = _make_clip()
    captured: dict[str, str] = {}
    calls = {"n": 0}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            calls["n"] += 1
            result = MagicMock()
            if calls["n"] == 1:
                result.scalar_one_or_none.return_value = clip
            elif calls["n"] == 2:
                result.scalar_one_or_none.return_value = None
            elif calls["n"] == 6:
                captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
                result.scalars.return_value.all.return_value = []
            else:
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth(role=UserRole.player)
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{clip.id}/overlays")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    where_clause = captured["sql"].split("WHERE", 1)[1]
    assert "metrics.experimental_flag" in where_clause
