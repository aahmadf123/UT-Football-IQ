"""Active-learning annotation queue tests (Issues #145 / #146).

Covers the pure prioritization policy (``app.active_learning``) and the
``GET /api/v1/corrections/queue`` endpoint: ordering by calibrated uncertainty,
the labeled-pool filter, safe handling of missing uncertainty, and the role gate.
Uses the standard mocked-AsyncSession pattern shared with other backend tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.active_learning import (
    DEFAULT_QUEUE_LIMIT,
    MAX_QUEUE_LIMIT,
    REASON_CALIBRATED,
    REASON_UNCALIBRATED,
    REASON_UNSCORED,
    UNSCORED_PRIORITY,
    effective_priority,
    normalize_limit,
    queue_reason,
)
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Clip, User, UserRole
from fastapi.testclient import TestClient

# ── Pure policy: effective_priority ───────────────────────────────────────────


def test_effective_priority_returns_clamped_score() -> None:
    assert effective_priority(0.7, True) == 0.7
    assert effective_priority(1.5, True) == 1.0
    assert effective_priority(-0.2, False) == 0.0


def test_missing_uncertainty_sorts_below_any_real_score() -> None:
    assert effective_priority(None, False) == UNSCORED_PRIORITY
    assert UNSCORED_PRIORITY < 0.0  # below the whole [0, 1] range


def test_calibration_does_not_change_priority_value() -> None:
    # Entropy is the AL signal regardless of calibration; the flag only governs
    # honest presentation, not ordering.
    assert effective_priority(0.6, True) == effective_priority(0.6, False)


def test_queue_priority_ordering_with_missing_scores() -> None:
    now = datetime.now(UTC)
    clips = [
        ("low", 0.2, True),
        ("high_uncal", 0.9, False),
        ("high_cal", 0.9, True),
        ("unscored", None, False),
    ]
    ordered = sorted(clips, key=lambda c: (effective_priority(c[1], c[2]), now), reverse=True)
    names = [c[0] for c in ordered]
    # Both 0.9 clips outrank 0.2; the unscored clip always sinks to the bottom.
    assert names[-1] == "unscored"
    assert set(names[:2]) == {"high_uncal", "high_cal"}
    assert names[2] == "low"


# ── Pure policy: reason + limit ───────────────────────────────────────────────


def test_queue_reason_classifies_each_case() -> None:
    assert queue_reason(None, False) == REASON_UNSCORED
    assert queue_reason(0.8, True) == REASON_CALIBRATED
    assert queue_reason(0.8, False) == REASON_UNCALIBRATED


def test_normalize_limit_clamps() -> None:
    assert normalize_limit(None) == DEFAULT_QUEUE_LIMIT
    assert normalize_limit(0) == 1
    assert normalize_limit(10_000) == MAX_QUEUE_LIMIT
    assert normalize_limit(5) == 5


# ── Endpoint helpers ──────────────────────────────────────────────────────────


def _make_user(role: UserRole = UserRole.coach) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _make_clip(
    *,
    uncertainty_score: float | None,
    uncertainty_calibrated: bool = False,
    is_reviewed: bool = False,
    play_number: int | None = 1,
) -> Clip:
    c = MagicMock(spec=Clip)
    c.id = uuid.uuid4()
    c.video_id = uuid.uuid4()
    c.play_number = play_number
    c.uncertainty_score = uncertainty_score
    c.uncertainty_calibrated = uncertainty_calibrated
    c.is_reviewed = is_reviewed
    c.created_at = datetime.now(UTC)
    return c


def _override_queue_db(rows: list[Clip], captured_sql: dict[str, str] | None = None) -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            if captured_sql is not None:
                captured_sql["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            result = MagicMock()
            result.scalars.return_value.all.return_value = rows
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    app.dependency_overrides[get_db] = _db


# ── Endpoint: ordering + reason classification ────────────────────────────────


def test_queue_returns_items_with_reason_and_priority() -> None:
    # Rows are returned in the order the SQL would produce them.
    rows = [
        _make_clip(uncertainty_score=0.91, uncertainty_calibrated=True),
        _make_clip(uncertainty_score=0.40, uncertainty_calibrated=False),
        _make_clip(uncertainty_score=None),
    ]
    captured: dict[str, str] = {}
    _override_queue_db(rows, captured)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/corrections/queue", params={"strategy": "uncertainty"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 3
    assert body[0]["reason"] == REASON_CALIBRATED
    assert body[0]["priority"] == 0.91
    assert body[1]["reason"] == REASON_UNCALIBRATED
    # Missing uncertainty is surfaced honestly, never as a confident score.
    assert body[2]["uncertainty_score"] is None
    assert body[2]["reason"] == REASON_UNSCORED
    assert body[2]["priority"] == UNSCORED_PRIORITY

    # The query excludes the labeled pool and orders by uncertainty.
    sql = captured["sql"]
    where = sql.split("WHERE", 1)[1]
    assert "clips.is_reviewed" in where
    assert "coach_corrections" in where  # NOT EXISTS correction subquery
    assert "uncertainty_score" in sql.split("ORDER BY", 1)[1]


def test_queue_recent_strategy_orders_by_created_at() -> None:
    captured: dict[str, str] = {}
    _override_queue_db([], captured)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/corrections/queue", params={"strategy": "recent"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    order_by = captured["sql"].split("ORDER BY", 1)[1]
    assert "created_at" in order_by
    assert "uncertainty_score" not in order_by


def test_queue_exclude_unscored_adds_not_null_filter() -> None:
    captured: dict[str, str] = {}
    _override_queue_db([], captured)
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/corrections/queue",
                params={"include_unscored": "false"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    where = captured["sql"].split("WHERE", 1)[1]
    assert "uncertainty_score IS NOT NULL" in where


def test_queue_rejects_unknown_strategy() -> None:
    _override_queue_db([])
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.coach)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/corrections/queue", params={"strategy": "bogus"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


# ── Endpoint: role gate ───────────────────────────────────────────────────────


def test_queue_denied_for_player_role() -> None:
    _override_queue_db([])
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.player)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/corrections/queue")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_queue_denied_for_viewer_role() -> None:
    _override_queue_db([])
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.viewer)
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/corrections/queue")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
