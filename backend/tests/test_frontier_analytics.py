"""Tests for frontier analytics (Issue #10): xSep / xYards / xPressure.

Covers:
  - metrics router forces frontier metric names experimental on ingest
  - GET /api/v1/analytics/frontier — player/viewer blocked, staff sees
    experimental metrics with definitions + caveats, bad metric name → 400
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.main import app
from app.models import Metric, User, UserRole
from fastapi.testclient import TestClient


def _make_user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    u.position_group = None
    return u


# ── metrics router: frontier names forced experimental ──────────────────────────


@pytest.mark.parametrize(
    "metric_name",
    ["xsep", "expected_completion", "expected_rushing_gain", "expected_yac", "contextual_pressure"],
)
@pytest.mark.parametrize("source", ["manual", "human", "real"])
def test_frontier_metric_forced_experimental_on_create(metric_name: str, source: str) -> None:
    from app.database import get_db
    from app.deps import get_current_user

    analyst = _make_user(UserRole.analyst)
    clip = MagicMock()
    clip.id = uuid.uuid4()

    added: list[Metric] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = clip
        session.execute = AsyncMock(return_value=result)

        def _add(obj: Any) -> None:
            added.append(obj)

        async def _flush() -> None:
            for o in added:
                if getattr(o, "created_at", None) is None:
                    o.created_at = datetime.now(UTC)

        session.add = MagicMock(side_effect=_add)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    app.dependency_overrides[get_current_user] = lambda: analyst
    app.dependency_overrides[get_db] = _db

    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/metrics",
            json={
                "clip_id": str(clip.id),
                "metric_name": metric_name,
                "metric_value": {"yards": 2.3, "source": "tracking", "sample_size": 12},
                # Worker (incorrectly) tries to publish it as trusted:
                "experimental_flag": False,
                "analytics_safe": True,
                "provenance": {"source": source, "validated": True},
            },
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    data = resp.json()
    # Forced experimental + never analytics_safe regardless of the payload.
    assert data["experimental_flag"] is True
    assert data["analytics_safe"] is False


# ── frontier read endpoint ──────────────────────────────────────────────────────


def _frontier_metric() -> MagicMock:
    m = MagicMock(spec=Metric)
    m.id = uuid.uuid4()
    m.clip_id = uuid.uuid4()
    m.tracklet_id = uuid.uuid4()
    m.metric_name = "xsep"
    m.metric_value = {
        "yards": 2.6,
        "source": "tracking",
        "sample_size": 14,
        "stability_note": "directional until ~30 reps",
    }
    m.unit = "yd"
    m.experimental_flag = True
    m.analytics_safe = False
    m.confidence = 0.6
    m.created_at = datetime(2026, 5, 9, tzinfo=UTC)
    return m


def test_frontier_player_blocked() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    player = _make_user(UserRole.player)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock())
        yield session

    app.dependency_overrides[get_current_user] = lambda: player
    app.dependency_overrides[get_db] = _db

    with TestClient(app) as c:
        resp = c.get("/api/v1/analytics/frontier")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_frontier_lists_experimental_with_definitions() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    analyst = _make_user(UserRole.analyst)
    metric = _frontier_metric()

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = [(metric, "WR")]
        session.execute = AsyncMock(return_value=result)
        yield session

    app.dependency_overrides[get_current_user] = lambda: analyst
    app.dependency_overrides[get_db] = _db

    with TestClient(app) as c:
        resp = c.get("/api/v1/analytics/frontier")
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["experimental"] is True
    assert data["total"] == 1
    item = data["metrics"][0]
    assert item["metric_name"] == "xsep"
    assert item["experimental"] is True
    assert item["analytics_safe"] is False
    assert item["source"] == "tracking"
    assert item["sample_size"] == 14
    assert item["position_group"] == "WR"
    # Definitions for all three frontier metrics are surfaced for the UI.
    assert set(data["definitions"]) == {"xsep", "xyards", "xpressure"}
    assert "NOT validated" in data["note"]


def test_frontier_all_filters_build_query() -> None:
    # Exercises every slice (position_group/formation/concept/opponent) so the
    # JSON ``.as_string()`` accessors + EXISTS subqueries are actually built —
    # a regression guard for the JSON-column query construction.
    from app.database import get_db
    from app.deps import get_current_user

    analyst = _make_user(UserRole.analyst)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        yield session

    app.dependency_overrides[get_current_user] = lambda: analyst
    app.dependency_overrides[get_db] = _db

    with TestClient(app) as c:
        resp = c.get(
            "/api/v1/analytics/frontier",
            params={
                "metric_name": "xsep",
                "position_group": "WR",
                "formation": "Trips",
                "concept": "mesh",
                "opponent": "Akron",
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_frontier_bad_metric_name_rejected() -> None:
    from app.database import get_db
    from app.deps import get_current_user

    analyst = _make_user(UserRole.analyst)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock())
        yield session

    app.dependency_overrides[get_current_user] = lambda: analyst
    app.dependency_overrides[get_db] = _db

    with TestClient(app) as c:
        resp = c.get("/api/v1/analytics/frontier", params={"metric_name": "xgoals"})
    app.dependency_overrides.clear()
    assert resp.status_code == 400
