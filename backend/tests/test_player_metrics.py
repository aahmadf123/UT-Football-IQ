"""Per-player metrics aggregation tests against real Postgres.

The summary endpoints are grouped SQL (span-weighted confidence, JSONB value
extraction, session/date filters), so they run against the configured test
database — same pattern as test_jobs_claim — creating only the FK-closure
tables they need and dropping what they created afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import (
    Clip,
    Metric,
    Player,
    SessionKind,
    Tracklet,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from app.routers.player_metrics import (
    get_player_metrics_summary,
    list_player_metrics_summaries,
)
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TABLES = [
    Base.metadata.tables["users"],
    Base.metadata.tables["players"],
    Base.metadata.tables["training_datasets"],
    Base.metadata.tables["model_versions"],
    Base.metadata.tables["videos"],
    Base.metadata.tables["field_calibrations"],
    Base.metadata.tables["clips"],
    Base.metadata.tables["processing_jobs"],
    Base.metadata.tables["tracklets"],
    Base.metadata.tables["metrics"],
]

_ENUM_TYPES = (
    "job_type",
    "job_status",
    "user_role",
    "video_status",
    "session_kind",
    "side_of_ball",
    "source_type",
    "capture_regime",
    "model_stage",
    "clip_result_state",
    "player_visibility_state",
)


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        existing: set[str] = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    created = [t for t in _TABLES if t.name not in existing]
    if created:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=created))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with engine.begin() as conn:
            # Deleting seeded videos cascades through clips → tracklets →
            # metrics; players are deleted explicitly.
            await conn.exec_driver_sql("DELETE FROM videos WHERE filename LIKE 'pm-test-%'")
            await conn.exec_driver_sql("DELETE FROM players WHERE last_name LIKE 'PMTest%'")
        if created:
            async with engine.begin() as conn:
                for table in reversed(created):
                    await conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
            for enum_name in _ENUM_TYPES:
                try:
                    async with engine.begin() as conn:
                        await conn.exec_driver_sql(f"DROP TYPE IF EXISTS {enum_name}")
                except Exception:
                    pass
        await engine.dispose()


def _staff_user() -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = UserRole.coach
    u.is_active = True
    return u


# The endpoints are called directly with a real session (TestClient would run
# the request in its own event loop, which asyncpg connections cannot cross).


def _metric(
    clip_id: uuid.UUID,
    tracklet_id: uuid.UUID | None,
    name: str,
    value: dict[str, Any],
    *,
    suppressed: bool = False,
    experimental: bool = False,
) -> Metric:
    return Metric(
        clip_id=clip_id,
        tracklet_id=tracklet_id,
        metric_name=name,
        metric_value=value,
        is_suppressed=suppressed,
        experimental_flag=experimental,
    )


async def _seed(maker: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    """Two players across a practice and a game video; one unattributed track."""
    async with maker() as session:
        p1 = Player(first_name="Alpha", last_name="PMTestOne", jersey_number=7)
        p2 = Player(first_name="Beta", last_name="PMTestTwo", jersey_number=21)
        p3 = Player(first_name="Gamma", last_name="PMTestThree", jersey_number=99)
        session.add_all([p1, p2, p3])
        await session.flush()

        v_practice = Video(
            filename="pm-test-practice.mp4",
            storage_uri="local://raw-video/pm-test-practice.mp4",
            status=VideoStatus.ready,
            session_kind=SessionKind.practice,
            recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        )
        v_game = Video(
            filename="pm-test-game.mp4",
            storage_uri="local://raw-video/pm-test-game.mp4",
            status=VideoStatus.ready,
            session_kind=SessionKind.game,
            recorded_at=datetime(2026, 8, 10, 19, 0, tzinfo=UTC),
        )
        session.add_all([v_practice, v_game])
        await session.flush()

        c_practice = Clip(
            video_id=v_practice.id,
            start_time=0.0,
            end_time=8.0,
            session_kind=SessionKind.practice,
        )
        c_game = Clip(
            video_id=v_game.id, start_time=0.0, end_time=8.0, session_kind=SessionKind.game
        )
        session.add_all([c_practice, c_game])
        await session.flush()

        # P1: long confident practice tracklet + short weak game tracklet.
        t1 = Tracklet(
            clip_id=c_practice.id,
            player_id=p1.id,
            start_frame=0,
            end_frame=99,
            track_confidence=0.9,
        )
        t2 = Tracklet(
            clip_id=c_game.id,
            player_id=p1.id,
            start_frame=0,
            end_frame=9,
            track_confidence=0.3,
        )
        # P2: a tracklet with no recorded confidence.
        t3 = Tracklet(
            clip_id=c_practice.id,
            player_id=p2.id,
            start_frame=0,
            end_frame=49,
            track_confidence=None,
        )
        # Unattributed tracklet must never leak into any player's numbers.
        t4 = Tracklet(
            clip_id=c_practice.id,
            player_id=None,
            start_frame=0,
            end_frame=49,
            track_confidence=0.99,
        )
        session.add_all([t1, t2, t3, t4])
        await session.flush()

        session.add_all(
            [
                _metric(c_practice.id, t1.id, "max_speed", {"yards_per_second": 8.5}),
                _metric(c_game.id, t2.id, "max_speed", {"yards_per_second": 6.0}),
                _metric(c_practice.id, t1.id, "distance_traveled", {"yards": 42.0}),
                # Excluded rows: suppressed, experimental, unattributed.
                _metric(
                    c_practice.id,
                    t1.id,
                    "max_speed",
                    {"yards_per_second": 99.0},
                    suppressed=True,
                ),
                _metric(
                    c_practice.id,
                    t1.id,
                    "max_speed",
                    {"yards_per_second": 88.0},
                    experimental=True,
                ),
                _metric(c_practice.id, t3.id, "distance_traveled", {"yards": 10.0}),
                _metric(c_practice.id, t4.id, "max_speed", {"yards_per_second": 12.0}),
            ]
        )
        await session.commit()
        return {"p1": p1.id, "p2": p2.id, "p3": p3.id}


async def _summaries(
    maker: async_sessionmaker[AsyncSession],
    session_kind: SessionKind | None = None,
    since: datetime | None = None,
) -> dict[uuid.UUID, Any]:
    async with maker() as session:
        rows = await list_player_metrics_summaries(
            session, _staff_user(), session_kind=session_kind, since=since
        )
    return {row.player_id: row for row in rows}


async def _detail(maker: async_sessionmaker[AsyncSession], player_id: uuid.UUID) -> Any:
    async with maker() as session:
        return await get_player_metrics_summary(
            player_id, session, _staff_user(), session_kind=None, since=None
        )


async def test_batched_summary_aggregates_and_excludes(
    db: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(db)
    by_id = await _summaries(db)

    # Only players with attributed tracklets appear.
    assert ids["p3"] not in by_id

    p1 = by_id[ids["p1"]]
    assert p1.tracklet_count == 2
    assert p1.tracked_clip_count == 2
    # Span-weighted: (0.9*100 + 0.3*10) / 110
    assert p1.identity_confidence is not None
    assert abs(p1.identity_confidence - 93 / 110) < 1e-6
    assert p1.identity_bucket.value == "probable"
    # Suppressed (99) and experimental (88) rows never win the max.
    assert p1.max_speed_yps == 8.5
    assert p1.max_speed_samples == 2
    assert p1.distance_yards == 42.0
    assert p1.distance_samples == 1
    assert p1.last_tracked_at is not None

    p2 = by_id[ids["p2"]]
    assert p2.identity_confidence is None
    assert p2.identity_bucket.value == "needs_review"
    assert p2.distance_yards == 10.0
    assert p2.max_speed_yps is None


async def test_session_kind_and_since_filters(db: async_sessionmaker[AsyncSession]) -> None:
    ids = await _seed(db)

    p1 = (await _summaries(db, session_kind=SessionKind.practice))[ids["p1"]]
    assert p1.tracklet_count == 1
    assert p1.identity_confidence is not None
    assert abs(p1.identity_confidence - 0.9) < 1e-6
    assert p1.max_speed_yps == 8.5

    p1g = (await _summaries(db, session_kind=SessionKind.game))[ids["p1"]]
    assert p1g.identity_confidence is not None
    assert abs(p1g.identity_confidence - 0.3) < 1e-6
    # 0.3 is below the profile identity threshold → honest bucket.
    assert p1g.identity_bucket.value == "needs_review"
    assert p1g.max_speed_yps == 6.0

    p1s = (await _summaries(db, since=datetime(2026, 8, 5, tzinfo=UTC)))[ids["p1"]]
    assert p1s.tracklet_count == 1
    assert p1s.tracked_clip_count == 1


async def test_detail_returns_weekly_series_and_empty_state(
    db: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(db)

    body = await _detail(db, ids["p1"])
    assert body.summary.tracklet_count == 2
    weeks = body.weekly
    assert len(weeks) == 2  # practice week and game week
    assert weeks[0].week_start < weeks[1].week_start
    assert weeks[0].identity_confidence is not None
    assert abs(weeks[0].identity_confidence - 0.9) < 1e-6
    assert weeks[0].max_speed_yps == 8.5
    assert weeks[0].distance_yards == 42.0
    assert weeks[1].max_speed_yps == 6.0
    assert weeks[1].distance_yards is None

    # A player with no tracked film gets the honest zero shape, not a 404.
    empty = await _detail(db, ids["p3"])
    assert empty.summary.tracklet_count == 0
    assert empty.summary.identity_confidence is None
    assert empty.summary.identity_bucket.value == "needs_review"
    assert empty.weekly == []
