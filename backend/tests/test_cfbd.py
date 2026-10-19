"""Tests for the cached CFBD analytics router (Issue #163).

Covers:
* Empty-cache, populated, stale-cache, and CFBD-unavailable cache states.
* Source labelling (CollegeFootballData.com).
* The ``cfbd_analytics:read`` policy gate (coaching staff only).
* Workload gating on the heavy MAC benchmark (distinguishable 503).

The router reads cached tables only, so the DB is faked with deterministic
rows per table — mirroring ``test_opponents`` / ``test_workload_gating``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import CfbdSyncStatus, User, UserRole
from fastapi.testclient import TestClient

NOW = datetime.now(UTC)


# ── Fixtures / helpers ─────────────────────────────────────────────────────────


def _make_user(role: UserRole = UserRole.analyst) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _sync_run(status: CfbdSyncStatus, finished_at: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        started_at=finished_at or NOW,
        finished_at=finished_at,
    )


def _make_db(
    *,
    rows_by_table: dict[str, list[Any]] | None = None,
    sync_latest: Any = None,
    sync_last_success: Any = None,
    workload_counts: list[int] | None = None,
):
    """Build a fake AsyncSession dispatching by the table referenced in the SQL."""
    rows_by_table = rows_by_table or {}
    counts = list(workload_counts or [])

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            sql = str(stmt).lower()
            result = MagicMock()

            # Workload gating count() queries (against processing_jobs).
            if "count(" in sql:
                result.scalar_one.return_value = counts.pop(0) if counts else 0
                return result

            if "cfbd_sync_runs" in sql:
                row = sync_last_success if "status in" in sql else sync_latest
                result.scalar_one_or_none.return_value = row
                scalars = MagicMock()
                scalars.all.return_value = [row] if row is not None else []
                result.scalars.return_value = scalars
                return result

            table = _table_of(sql)
            rows = rows_by_table.get(table, [])
            result.scalar_one_or_none.return_value = rows[0] if rows else None
            scalars = MagicMock()
            scalars.all.return_value = rows
            result.scalars.return_value = scalars
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    return _db


def _table_of(sql: str) -> str:
    for name in (
        "cfbd_teams",
        "cfbd_games",
        "cfbd_plays",
        "cfbd_drives",
        "cfbd_win_probability",
        "cfbd_team_game_stats",
    ):
        if name in sql:
            return name
    return ""


@contextmanager
def _client(db_factory, *, user: User | None = None) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = lambda: user or _make_user()
    app.dependency_overrides[get_db] = db_factory
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


# ── Cache-state tests ──────────────────────────────────────────────────────────


def test_toledo_team_empty_cache() -> None:
    with _client(_make_db()) as c:
        resp = c.get("/api/cfbd/teams/toledo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["team"] is None
    assert body["cache"]["sync_status"] == "never"
    assert body["cache"]["stale"] is True
    assert body["cache"]["row_count"] == 0
    assert body["cache"]["source"] == "CollegeFootballData.com"


def test_toledo_team_populated_fresh() -> None:
    team = SimpleNamespace(
        cfbd_team_id=2649,
        school="Toledo",
        mascot="Rockets",
        abbreviation="TOL",
        conference="Mid-American",
        division="West",
        color="#15397F",
        alt_color="#FFD20A",
    )
    db = _make_db(
        rows_by_table={"cfbd_teams": [team]},
        sync_latest=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(hours=1)),
        sync_last_success=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(hours=1)),
    )
    with _client(db) as c:
        resp = c.get("/api/cfbd/teams/toledo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["team"]["school"] == "Toledo"
    assert body["team"]["mascot"] == "Rockets"
    assert body["cache"]["sync_status"] == "ok"
    assert body["cache"]["stale"] is False
    assert body["cache"]["last_synced_at"] is not None


def test_schedule_stale_cache_warning() -> None:
    game = SimpleNamespace(
        cfbd_game_id=401,
        season=2025,
        week=1,
        season_type="regular",
        start_date=datetime(2025, 8, 30, tzinfo=UTC),
        home_team="Toledo",
        away_team="Akron",
        home_points=31,
        away_points=10,
        venue="Glass Bowl",
        completed=True,
    )
    db = _make_db(
        rows_by_table={"cfbd_games": [game]},
        sync_latest=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(days=30)),
        sync_last_success=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(days=30)),
    )
    with _client(db) as c:
        resp = c.get("/api/cfbd/teams/toledo/schedule?season=2025")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["games"]) == 1
    assert body["games"][0]["home_team"] == "Toledo"
    # Last successful sync is 30 days old (> 168h default) -> stale.
    assert body["cache"]["stale"] is True
    assert body["cache"]["sync_status"] == "ok"


def test_schedule_cfbd_unavailable() -> None:
    db = _make_db(
        rows_by_table={"cfbd_games": []},
        sync_latest=_sync_run(CfbdSyncStatus.error, None),
        sync_last_success=None,
    )
    with _client(db) as c:
        resp = c.get("/api/cfbd/teams/toledo/schedule")
    assert resp.status_code == 200
    body = resp.json()
    assert body["games"] == []
    assert body["cache"]["sync_status"] == "error"
    assert body["cache"]["stale"] is True
    assert body["cache"]["last_synced_at"] is None


def test_game_plays_populated() -> None:
    play = SimpleNamespace(
        cfbd_play_id=1,
        cfbd_drive_id=10,
        offense="Toledo",
        defense="Akron",
        period=1,
        clock="14:32",
        down=1,
        distance=10,
        yard_line=25,
        yards_gained=7,
        play_type="Rush",
        play_text="Run for 7 yards",
        ppa=0.21,
    )
    db = _make_db(
        rows_by_table={"cfbd_plays": [play]},
        sync_latest=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(hours=2)),
        sync_last_success=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(hours=2)),
    )
    with _client(db) as c:
        resp = c.get("/api/cfbd/games/401/plays")
    assert resp.status_code == 200
    body = resp.json()
    assert body["game_id"] == 401
    assert len(body["plays"]) == 1
    assert body["plays"][0]["play_type"] == "Rush"
    assert body["cache"]["stale"] is False


def test_game_drives_and_win_probability_empty() -> None:
    db = _make_db()
    with _client(db) as c:
        drives = c.get("/api/cfbd/games/401/drives")
        wp = c.get("/api/cfbd/games/401/win-probability")
    assert drives.status_code == 200
    assert drives.json()["drives"] == []
    assert wp.status_code == 200
    assert wp.json()["timeline"] == []


def test_mac_benchmark_aggregates() -> None:
    rows = [
        SimpleNamespace(
            cfbd_game_id=1,
            team="Toledo",
            conference="Mid-American",
            opponent="Akron",
            points=31,
            stats=[],
        ),
        SimpleNamespace(
            cfbd_game_id=2,
            team="Toledo",
            conference="Mid-American",
            opponent="Ball State",
            points=24,
            stats=[],
        ),
        SimpleNamespace(
            cfbd_game_id=1,
            team="Akron",
            conference="Mid-American",
            opponent="Toledo",
            points=10,
            stats=[],
        ),
        SimpleNamespace(
            cfbd_game_id=2,
            team="Ball State",
            conference="Mid-American",
            opponent="Toledo",
            points=20,
            stats=[],
        ),
    ]
    db = _make_db(
        rows_by_table={"cfbd_team_game_stats": rows},
        sync_latest=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(hours=3)),
        sync_last_success=_sync_run(CfbdSyncStatus.ok, NOW - timedelta(hours=3)),
        workload_counts=[0, 0],
    )
    with _client(db) as c:
        resp = c.get("/api/cfbd/mac/benchmark?season=2025")
    assert resp.status_code == 200
    body = resp.json()
    teams = {t["team"]: t for t in body["teams"]}
    assert teams["Toledo"]["games"] == 2
    assert teams["Toledo"]["avg_points_for"] == 27.5
    assert teams["Toledo"]["avg_points_against"] == 15.0
    assert teams["Toledo"]["point_differential"] == 12.5
    # Sorted best differential first.
    assert body["teams"][0]["team"] == "Toledo"
    assert body["conference"] == "Mid-American"


def test_mac_benchmark_workload_gated() -> None:
    db = _make_db(
        rows_by_table={"cfbd_team_game_stats": []},
        workload_counts=[510, 0],  # queued >= default threshold 500 -> saturated
    )
    with _client(db) as c:
        resp = c.get("/api/cfbd/mac/benchmark")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_code"] == "workload_gated"


# ── Policy tests ───────────────────────────────────────────────────────────────


def test_cfbd_requires_staff_policy() -> None:
    with _client(_make_db(), user=_make_user(UserRole.viewer)) as c:
        resp = c.get("/api/cfbd/teams/toledo")
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"


def test_cfbd_allows_coach() -> None:
    with _client(_make_db(), user=_make_user(UserRole.coach)) as c:
        resp = c.get("/api/cfbd/teams/toledo")
    assert resp.status_code == 200


def test_cfbd_film_link_requires_admin() -> None:
    with _client(_make_db(), user=_make_user(UserRole.sportsperformance)) as c:
        resp = c.post(f"/api/cfbd/videos/{uuid.uuid4()}/link")
    assert resp.status_code == 403
