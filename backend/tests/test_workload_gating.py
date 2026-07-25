"""Workload gating tests (Issue #113).

Covers :mod:`app.workload`:

* ``assess_workload`` classification thresholds.
* ``require_workload_capacity`` rejecting heavy work under saturation with a
  503 ``workload_gated`` payload.
* ``GET /api/v1/health/workload`` RBAC gate.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from app.workload import (
    DEFAULT_QUEUE_THRESHOLD,
    WorkloadStatus,
    _classify,
)
from fastapi.testclient import TestClient


def _make_user(role: UserRole = UserRole.admin) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _override_workload_counts(*, queued: int, running: int) -> None:
    """Wire ``get_db`` so workload counts are deterministic.

    The router (``jobs.create_job`` etc.) also issues other queries.  We make
    the AsyncSession return ``queued`` then ``running`` for the first two
    ``select(count())`` calls and ``None``/``MagicMock`` for everything else
    so endpoint-specific lookups still succeed.
    """
    counts = [queued, running]

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            if counts:
                result.scalar_one.return_value = counts.pop(0)
            else:
                result.scalar_one.return_value = 0
            result.scalar_one_or_none.return_value = None
            scalars = MagicMock()
            scalars.all.return_value = []
            result.scalars.return_value = scalars
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[get_db] = _db


# ── Classification ───────────────────────────────────────────────────────────


def test_classify_healthy_under_thresholds() -> None:
    assert (
        _classify(queued=1, running=1, queue_threshold=50, running_threshold=20)
        == WorkloadStatus.healthy
    )


def test_classify_degraded_at_75_percent() -> None:
    # 38 / 50 = 76% -> degraded
    assert (
        _classify(queued=38, running=0, queue_threshold=50, running_threshold=20)
        == WorkloadStatus.degraded
    )


def test_classify_saturated_at_threshold() -> None:
    assert (
        _classify(queued=50, running=0, queue_threshold=50, running_threshold=20)
        == WorkloadStatus.saturated
    )
    assert (
        _classify(queued=0, running=25, queue_threshold=50, running_threshold=20)
        == WorkloadStatus.saturated
    )


# ── Gating dependency ────────────────────────────────────────────────────────


def test_jobs_create_returns_workload_gated_when_saturated() -> None:
    _override_workload_counts(queued=DEFAULT_QUEUE_THRESHOLD + 10, running=0)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/jobs",
                json={
                    "video_id": str(uuid.uuid4()),
                    "job_type": "detect",
                    "priority": 0,
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error_code"] == "workload_gated"
    assert body["detail"]["endpoint"] == "jobs.create"
    assert resp.headers["Retry-After"] == "30"


def test_jobs_create_allowed_under_normal_load() -> None:
    # Configure a video to exist so the handler runs to completion.
    counts = [0, 0]  # queued, running

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        call_idx = {"n": 0}

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            if counts:
                result.scalar_one.return_value = counts.pop(0)
            else:
                result.scalar_one.return_value = 0
            # Third query (after the two count() queries) is the Video lookup.
            call_idx["n"] += 1
            video = MagicMock()
            video.id = uuid.uuid4()
            result.scalar_one_or_none.return_value = video if call_idx["n"] >= 3 else None
            scalars = MagicMock()
            scalars.all.return_value = []
            result.scalars.return_value = scalars
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        # add() is a regular method, not async.
        session.add = MagicMock()
        yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        # raise_server_exceptions=False lets the AttributeError surfaced by
        # serializing the MagicMock job become a 500 instead of bubbling out.
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/api/v1/jobs",
                json={
                    "video_id": str(uuid.uuid4()),
                    "job_type": "detect",
                    "priority": 0,
                },
            )
    finally:
        app.dependency_overrides.clear()
    # Anything other than 503 proves the gating layer did not reject the
    # request before the handler ran.
    assert resp.status_code != 503


def test_jobs_create_bypasses_gating_when_env_disabled(monkeypatch: Any) -> None:
    from app.config import get_settings

    monkeypatch.setenv("WORKLOAD_GATING_DISABLED", "true")
    # Settings are cached; drop it so this value is the one that gets read.
    # The autouse fixture in conftest clears it again on the way out.
    get_settings.cache_clear()
    _override_workload_counts(queued=10_000, running=10_000)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.post(
                "/api/v1/jobs",
                json={
                    "video_id": str(uuid.uuid4()),
                    "job_type": "detect",
                    "priority": 0,
                },
            )
    finally:
        app.dependency_overrides.clear()
        os.environ.pop("WORKLOAD_GATING_DISABLED", None)
        get_settings.cache_clear()
    # Should pass the gate even at extreme load; downstream may 404/500
    # because the video doesn't exist — both confirm gating was skipped.
    assert resp.status_code != 503


# ── Health/workload endpoint ─────────────────────────────────────────────────


def test_health_workload_requires_sportsperformance_or_above() -> None:
    _override_workload_counts(queued=0, running=0)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/health/workload")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"


def test_health_workload_returns_snapshot_for_sportsperformance() -> None:
    _override_workload_counts(queued=2, running=1)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/health/workload")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued"] == 2
    assert body["running"] == 1
    assert body["status"] == "healthy"
    assert "queue_threshold" in body
