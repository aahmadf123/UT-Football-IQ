"""Integration tests for CFBD → Postgres ingestion (Issue #162).

These exercise the real upsert path against the configured Postgres (the same
DB the rest of the suite and CI use), creating only the CFBD cache tables so we
can assert idempotency, sync-run tracking, partial-failure isolation, and that
cached data is queryable without any further CFBD call.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest_asyncio
from app.cfbd import models as m
from app.cfbd import sync as sync_module
from app.cfbd.client import CFBDClient
from app.cfbd.models import CFBDSyncStatus
from app.cfbd.sync import sync_season
from app.config import get_settings
from app.database import Base
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TABLES = [
    m.CFBDTeam.__table__,
    m.CFBDGame.__table__,
    m.CFBDDrive.__table__,
    m.CFBDPlay.__table__,
    m.CFBDTeamGameStat.__table__,
    m.CFBDWinProbability.__table__,
    m.CFBDSyncRun.__table__,
]

_GAMES = [
    {
        "id": 401,
        "season": 2024,
        "week": 1,
        "seasonType": "regular",
        "startDate": "2024-08-31T16:00:00.000Z",
        "venue": "Glass Bowl",
        "homeTeam": "Toledo",
        "homeConference": "Mid-American",
        "homePoints": 30,
        "awayTeam": "Ball State",
        "awayConference": "Mid-American",
        "awayPoints": 20,
        "completed": True,
    },
    {
        "id": 402,
        "season": 2024,
        "week": 2,
        "seasonType": "regular",
        "startDate": "2024-09-07T16:00:00.000Z",
        "venue": "Doyt Perry Stadium",
        "homeTeam": "Bowling Green",
        "homeConference": "Mid-American",
        "homePoints": 17,
        "awayTeam": "Toledo",
        "awayConference": "Mid-American",
        "awayPoints": 24,
        "completed": True,
    },
]


def _ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _good_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/teams":
        return _ok(
            [
                {"id": 1, "school": "Toledo", "conference": "Mid-American", "mascot": "Rockets"},
                {"id": 2, "school": "Ball State", "conference": "Mid-American"},
            ]
        )
    if path == "/games":
        return _ok(_GAMES)
    if path == "/drives":
        return _ok(
            [
                {
                    "id": 900,
                    "gameId": 401,
                    "offense": "Toledo",
                    "defense": "Ball State",
                    "scoring": True,
                    "plays": 8,
                    "yards": 75,
                    "driveResult": "TD",
                },
                {
                    "id": 901,
                    "gameId": 402,
                    "offense": "Toledo",
                    "defense": "Bowling Green",
                    "scoring": False,
                    "plays": 5,
                    "yards": 20,
                    "driveResult": "PUNT",
                },
            ]
        )
    if path == "/plays":
        week = request.url.params.get("week")
        if week == "1":
            return _ok(
                [
                    {
                        "id": 1000,
                        "gameId": 401,
                        "driveId": 900,
                        "offense": "Toledo",
                        "defense": "Ball State",
                        "playType": "Rush",
                        "yardsGained": 5,
                        "down": 1,
                    },
                ]
            )
        return _ok([])
    if path == "/games/teams":
        return _ok(
            [
                {
                    "id": 401,
                    "teams": [
                        {
                            "team": "Toledo",
                            "homeAway": "home",
                            "points": 30,
                            "stats": [{"category": "rushingYards", "stat": "210"}],
                        },
                        {"team": "Ball State", "homeAway": "away", "points": 20, "stats": []},
                    ],
                },
                {
                    "id": 402,
                    "teams": [
                        {"team": "Toledo", "homeAway": "away", "points": 24, "stats": []},
                        {"team": "Bowling Green", "homeAway": "home", "points": 17, "stats": []},
                    ],
                },
            ]
        )
    if path == "/metrics/wp":
        game_id = request.url.params.get("gameId")
        if game_id == "401":
            return _ok(
                [
                    {"gameId": 401, "playNumber": 1, "homeWinProb": 0.50, "homeBall": True},
                    {"gameId": 401, "playNumber": 2, "homeWinProb": 0.61, "homeBall": True},
                ]
            )
        return _ok([{"gameId": 402, "playNumber": 1, "homeWinProb": 0.45, "homeBall": False}])
    return _ok([])


async def _noop_sleep(_seconds: float) -> None:
    pass


def _client(handler: object) -> CFBDClient:
    return CFBDClient(
        "test-key-never-real",
        base_url="https://api.example.test",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        backoff_base=0.0,
        max_retries=1,
        sleep=_noop_sleep,
    )


@pytest_asyncio.fixture
async def cfbd_session() -> AsyncGenerator[AsyncSession, None]:
    # Sync history is cumulative; each test gets a private schema so repeat
    # runs never mistake previous runs' audit rows for new network activity.
    schema = f"cfbd_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(get_settings().database_url)
    async with admin_engine.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        get_settings().database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_TABLES, checkfirst=False))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin_engine.dispose()


async def _count(session: AsyncSession, model: type[object]) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def test_sync_season_populates_all_tables(cfbd_session: AsyncSession) -> None:
    async with _client(_good_handler) as client:
        summary = await sync_season(cfbd_session, client, season=2024)

    assert all(r.status == CFBDSyncStatus.succeeded for r in summary.results.values())
    assert await _count(cfbd_session, m.CFBDTeam) == 2
    assert await _count(cfbd_session, m.CFBDGame) == 2
    assert await _count(cfbd_session, m.CFBDDrive) == 2
    assert await _count(cfbd_session, m.CFBDPlay) == 1
    assert await _count(cfbd_session, m.CFBDTeamGameStat) == 4
    assert await _count(cfbd_session, m.CFBDWinProbability) == 3
    # One sync-run row per endpoint family.
    assert await _count(cfbd_session, m.CFBDSyncRun) == 6

    # Opponent is derived for the Toledo stat row.
    toledo_stat = (
        await cfbd_session.execute(
            select(m.CFBDTeamGameStat).where(
                m.CFBDTeamGameStat.cfbd_game_id == 401,
                m.CFBDTeamGameStat.team == "Toledo",
            )
        )
    ).scalar_one()
    assert toledo_stat.opponent == "Ball State"
    assert toledo_stat.home_away == "home"


async def test_sync_is_idempotent(cfbd_session: AsyncSession) -> None:
    async with _client(_good_handler) as client:
        await sync_season(cfbd_session, client, season=2024)
        await sync_season(cfbd_session, client, season=2024)

    # Row counts are unchanged after a second run (upsert, not duplicate).
    assert await _count(cfbd_session, m.CFBDGame) == 2
    assert await _count(cfbd_session, m.CFBDTeam) == 2
    assert await _count(cfbd_session, m.CFBDWinProbability) == 3
    # Every attempt is still recorded for auditing (6 endpoints x 2 runs).
    assert await _count(cfbd_session, m.CFBDSyncRun) == 12


async def test_partial_failure_is_isolated_and_recorded(
    cfbd_session: AsyncSession,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drives":
            return httpx.Response(503)  # upstream outage for one endpoint only
        return _good_handler(request)

    async with _client(handler) as client:
        summary = await sync_season(cfbd_session, client, season=2024)

    assert summary.results["drives"].status == CFBDSyncStatus.failed
    assert summary.results["games"].status == CFBDSyncStatus.succeeded
    # The failed endpoint wrote nothing; the others still cached their data.
    assert await _count(cfbd_session, m.CFBDDrive) == 0
    assert await _count(cfbd_session, m.CFBDGame) == 2

    failed_run = (
        await cfbd_session.execute(select(m.CFBDSyncRun).where(m.CFBDSyncRun.endpoint == "/drives"))
    ).scalar_one()
    assert failed_run.status == CFBDSyncStatus.failed
    assert failed_run.error_summary is not None
    # No vendor key ever lands in the sync-run record.
    assert "test-key-never-real" not in (failed_run.error_summary or "")


async def test_cached_data_readable_without_cfbd(cfbd_session: AsyncSession) -> None:
    async with _client(_good_handler) as client:
        await sync_season(cfbd_session, client, season=2024)

    # No client involved here — pure cache read, proving availability when CFBD
    # is down.
    games = (
        await cfbd_session.execute(
            select(m.CFBDGame.home_team, m.CFBDGame.away_team)
            .where(m.CFBDGame.season == 2024)
            .order_by(m.CFBDGame.week)
        )
    ).all()
    assert ("Toledo", "Ball State") in [(g[0], g[1]) for g in games]


async def test_upsert_batches_rows_below_driver_parameter_limit(
    cfbd_session: AsyncSession,
) -> None:
    # 3,300 rows x 10 columns = 33,000 parameters, above asyncpg's 32,767 cap.
    rows = [
        {
            "cfbd_team_id": team_id,
            "school": f"School {team_id}",
            "mascot": None,
            "abbreviation": None,
            "conference": "Mid-American",
            "division": None,
            "classification": "fbs",
            "color": None,
            "alt_color": None,
            "logos": [],
        }
        for team_id in range(1, 3301)
    ]
    written = await sync_module._upsert(
        cfbd_session, m.CFBDTeam, rows, conflict_cols=["cfbd_team_id"]
    )
    await cfbd_session.commit()

    assert written == len(rows)
    assert await _count(cfbd_session, m.CFBDTeam) == len(rows)
