"""Team dashboard aggregation (needs Postgres: percentile_cont, JSONB)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import (
    Clip,
    Metric,
    Player,
    PlayerWorkloadDaily,
    SessionKind,
    SideOfBall,
    Tracklet,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from app.routers.team_dashboard import team_dashboard
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TABLES = [
    Base.metadata.tables[name]
    for name in (
        "users",
        "players",
        "training_datasets",
        "model_versions",
        "football_sessions",
        "videos",
        "field_calibrations",
        "clips",
        "processing_jobs",
        "tracklets",
        "metrics",
        "player_workload_daily",
    )
]


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
            await conn.exec_driver_sql("DELETE FROM videos WHERE filename LIKE 'td-test-%'")
            await conn.exec_driver_sql(
                "DELETE FROM player_workload_daily WHERE player_id IN "
                "(SELECT id FROM players WHERE last_name LIKE 'TDTest%')"
            )
            await conn.exec_driver_sql("DELETE FROM players WHERE last_name LIKE 'TDTest%'")
        await engine.dispose()


def _user(role: UserRole) -> User:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.role = role
    return user


def _profile(top: float, hsy: float, get_off: float | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "yards": 20.0,
        "seconds": 5.0,
        "top_speed_yps": top,
        "high_speed_yards": hsy,
        "sprint_yards": 2.0,
        "sprints": 1,
    }
    if get_off is not None:
        value["get_off_yards"] = get_off
    return value


async def _seed(maker: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    async with maker() as session:
        wr = Player(
            first_name="Fast", last_name="TDTestWR", jersey_number=1, position_group="Skill"
        )
        lb = Player(first_name="Hit", last_name="TDTestLB", jersey_number=44, position_group="LB")
        session.add_all([wr, lb])
        await session.flush()

        def video(name: str, kind: SessionKind, when: dt.datetime) -> Video:
            return Video(
                filename=f"td-test-{name}.mp4",
                storage_uri=f"local://raw-video/td-{name}.mp4",
                status=VideoStatus.ready,
                session_kind=kind,
                recorded_at=when,
            )

        last_week = now - dt.timedelta(days=7)
        practice_old = video("practice-old", SessionKind.practice, last_week)
        practice_new = video("practice-new", SessionKind.practice, now)
        game = video("game", SessionKind.game, now)
        session.add_all([practice_old, practice_new, game])
        await session.flush()

        def clip(v: Video, possession: SideOfBall | None = None) -> Clip:
            return Clip(
                video_id=v.id,
                start_time=0.0,
                end_time=8.0,
                session_kind=v.session_kind,
                our_possession=possession,
            )

        c_old, c_new = clip(practice_old), clip(practice_new)
        c_game = clip(game, SideOfBall.defense)
        session.add_all([c_old, c_new, c_game])
        await session.flush()

        def track(c: Clip, label: str, player: Player | None = None, group: str | None = None):
            return Tracklet(
                clip_id=c.id,
                player_id=player.id if player else None,
                start_frame=0,
                end_frame=99,
                team_label=label,
                position_group=group,
            )

        t_wr_old = track(c_old, "offense", wr)
        t_wr_new = track(c_new, "offense", wr)
        t_unid = track(c_new, "defense", group="DB")  # unidentified Toledo DB
        t_toledo_game = track(c_game, "defense", lb)  # Toledo on defense
        t_opponent = track(c_game, "offense")  # the opponent's offense
        session.add_all([t_wr_old, t_wr_new, t_unid, t_toledo_game, t_opponent])
        await session.flush()

        def metric(t: Tracklet, value: dict[str, Any]) -> Metric:
            return Metric(
                clip_id=t.clip_id,
                tracklet_id=t.id,
                metric_name="physical_profile",
                metric_value=value,
            )

        session.add_all(
            [
                metric(t_wr_old, _profile(8.0, 6.0, 2.0)),
                metric(t_wr_new, _profile(9.0, 10.0, 2.5)),
                metric(t_unid, _profile(7.0, 4.0)),
                metric(t_toledo_game, _profile(6.5, 3.0)),
                metric(t_opponent, _profile(11.0, 30.0)),  # must not count
            ]
        )
        today = now.date()
        session.add_all(
            [
                PlayerWorkloadDaily(player_id=wr.id, date=today, acwr=1.6, injury_risk_score=0.72),
                PlayerWorkloadDaily(player_id=lb.id, date=today, acwr=1.0, injury_risk_score=0.2),
            ]
        )
        await session.commit()
        return {"wr": wr.id, "lb": lb.id}


async def _dashboard(maker, role: UserRole):
    async with maker() as session:
        return await team_dashboard(session, _user(role), weeks=4, session_kind=None)


async def test_weekly_team_rates_count_toledo_tracks_only(db) -> None:
    await _seed(db)
    out = await _dashboard(db, UserRole.analyst)
    by_key = {(w.week_start, w.session_kind): w for w in out.weekly}
    game_weeks = [w for (week, kind), w in by_key.items() if kind == "game"]
    assert len(game_weeks) == 1
    # Only Toledo's defense counts in the game; the opponent's 11 yd/s is out.
    assert game_weeks[0].reps == 1 and game_weeks[0].top_speed_p90_yps == pytest.approx(6.5)
    practice = [w for (week, kind), w in by_key.items() if kind == "practice"]
    assert sum(w.reps for w in practice) == 3  # identified WR twice + unidentified DB


async def test_position_groups_show_week_over_week_change(db) -> None:
    await _seed(db)
    out = await _dashboard(db, UserRole.analyst)
    groups = {g.position_group: g for g in out.position_groups}
    assert set(groups) >= {"SKILL", "LB", "DB"}  # DB comes from the track's own role
    skill = groups["SKILL"]
    assert len(skill.weekly) == 2
    assert skill.change["top_speed_p90_yps"] == pytest.approx(1.0)
    assert skill.change["high_speed_yards_per_rep"] == pytest.approx(4.0)
    assert groups["LB"].change["top_speed_p90_yps"] is None  # one week only


async def test_readiness_follows_the_health_workload_roles(db) -> None:
    ids = await _seed(db)
    coach = await _dashboard(db, UserRole.coach)
    assert coach.readiness.visible is False and coach.readiness.groups == []

    analyst = await _dashboard(db, UserRole.analyst)
    assert analyst.readiness.visible is True and analyst.readiness.player_level is False
    assert analyst.readiness.flagged == []
    assert {g["position_group"] for g in analyst.readiness.groups} >= {"SKILL", "LB"}

    staff = await _dashboard(db, UserRole.sportsperformance)
    assert [f["player_id"] for f in staff.readiness.flagged] == [str(ids["wr"])]


async def test_activity_counts_this_weeks_film(db) -> None:
    await _seed(db)
    out = await _dashboard(db, UserRole.coach)
    assert out.activity.plays_this_week >= 2
    assert out.games == []  # the seeded game has no CFBD-linked plays
