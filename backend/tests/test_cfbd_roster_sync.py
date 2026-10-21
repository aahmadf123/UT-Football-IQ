"""CFBD roster → players, and per-player season data (real Postgres).

Covers: first sync creates the roster with CFBD bio fields; a re-sync is a
no-op; a coach's locked edit (practice jersey) survives; players created
before CFBD are adopted by name instead of duplicated; duplicate numbers in
one position group don't abort the sync; season stats, usage and PPA merge
into one row per athlete.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest_asyncio
from app.cfbd import models as m
from app.cfbd.client import CFBDClient
from app.cfbd.roster_sync import position_group, sync_player_seasons, sync_roster
from app.config import get_settings
from app.database import Base
from app.models import Player, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_TABLES = [
    User.__table__,
    Player.__table__,
    m.CFBDPlayerSeason.__table__,
    m.CFBDSyncRun.__table__,
]

_ROSTER: list[dict[str, Any]] = [
    {
        "id": 101,
        "firstName": "Khamoni",
        "lastName": "Robinson",
        "jersey": 7,
        "position": "QB",
        "height": 73,
        "weight": 195,
        "year": 3,
        "homeCity": "Monroe",
        "homeState": "NC",
        "homeCountry": "USA",
    },
    {"id": 102, "firstName": "Anthony", "lastName": "Simpson", "jersey": 85, "position": "WR"},
    {"id": 103, "firstName": "Armorion", "lastName": "Smith", "jersey": 7, "position": "S"},
    # Same number and group as Smith: must not abort the sync.
    {"id": 104, "firstName": "Late", "lastName": "Addition", "jersey": 7, "position": "CB"},
]


async def _noop_sleep(_seconds: float) -> None:
    pass


def _client(roster: list[dict[str, Any]]) -> CFBDClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/roster":
            return httpx.Response(200, json=roster)
        if path == "/stats/player/season":
            return httpx.Response(
                200,
                json=[
                    {
                        "playerId": "101",
                        "player": "Khamoni Robinson",
                        "position": "QB",
                        "team": "Toledo",
                        "category": "passing",
                        "statType": "YDS",
                        "stat": "2104",
                    },
                    {
                        "playerId": "101",
                        "player": "Khamoni Robinson",
                        "position": "QB",
                        "team": "Toledo",
                        "category": "rushing",
                        "statType": "CAR",
                        "stat": "41",
                    },
                ],
            )
        if path == "/player/usage":
            return httpx.Response(
                200, json=[{"id": "101", "name": "Khamoni Robinson", "usage": {"overall": 0.4}}]
            )
        if path == "/ppa/players/season":
            return httpx.Response(
                200,
                json=[
                    {"id": "101", "averagePPA": {"all": 0.21}, "totalPPA": {"all": 80.5}},
                ],
            )
        return httpx.Response(404)

    return CFBDClient(
        "test-key-never-real",
        base_url="https://api.example.test",
        transport=httpx.MockTransport(handler),
        backoff_base=0.0,
        max_retries=1,
        sleep=_noop_sleep,
    )


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    schema = f"roster_test_{uuid.uuid4().hex}"
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
        async with maker() as db:
            yield db
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin_engine.dispose()


async def _players(db: AsyncSession) -> dict[str, Player]:
    rows = (await db.execute(select(Player).execution_options(populate_existing=True))).scalars()
    return {f"{p.first_name} {p.last_name}": p for p in rows}


def test_position_groups() -> None:
    assert position_group("qb") == "QB"
    assert position_group("CB") == "DB"
    assert position_group("EDGE") == "DL"
    assert position_group(None) is None
    assert position_group("??") is None


async def test_first_sync_creates_roster_with_bio(session: AsyncSession) -> None:
    async with _client(_ROSTER) as client:
        result = await sync_roster(session, client, season=2026)
    assert result.status == m.CFBDSyncStatus.succeeded
    players = await _players(session)
    assert len(players) == 4
    qb = players["Khamoni Robinson"]
    assert (qb.jersey_number, qb.position, qb.position_group) == (7, "QB", "QB")
    assert qb.metadata_ is not None
    assert qb.metadata_["cfbd_athlete_id"] == "101"
    assert qb.metadata_["class_year"] == "Jr."
    assert qb.metadata_["hometown"] == "Monroe, NC"
    assert qb.metadata_["height_in"] == 73
    # Duplicate numbers are retained as distinct identity candidates.
    assert players["Armorion Smith"].jersey_number == 7
    assert players["Late Addition"].jersey_number == 7


async def test_resync_is_a_noop_and_keeps_coach_edits(session: AsyncSession) -> None:
    async with _client(_ROSTER) as client:
        await sync_roster(session, client, season=2026)
    players = await _players(session)
    wr = players["Anthony Simpson"]
    # A coach gave him a practice number via the roster editor.
    wr.jersey_number = 1
    wr.metadata_ = {**(wr.metadata_ or {}), "staff_locked_fields": ["jersey_number"]}
    await session.commit()

    async with _client(_ROSTER) as client:
        await sync_roster(session, client, season=2026)
    players = await _players(session)
    assert len(players) == 4
    assert players["Anthony Simpson"].jersey_number == 1
    assert players["Khamoni Robinson"].jersey_number == 7


async def test_adopts_players_created_before_cfbd(session: AsyncSession) -> None:
    session.add(
        Player(
            first_name="Khamoni",
            last_name="Robinson",
            jersey_number=7,
            position="QB",
            position_group="QB",
            metadata_={"roster_key": "2026-robinson-khamoni"},
            is_active=True,
        )
    )
    await session.commit()
    async with _client(_ROSTER) as client:
        await sync_roster(session, client, season=2026)
    count = (await session.execute(select(func.count()).select_from(Player))).scalar_one()
    assert count == 4
    qb = (await _players(session))["Khamoni Robinson"]
    assert qb.metadata_ is not None and qb.metadata_["cfbd_athlete_id"] == "101"


async def test_player_seasons_merge_stats_usage_and_ppa(session: AsyncSession) -> None:
    async with _client(_ROSTER) as client:
        result = await sync_player_seasons(session, client, season=2025)
        # Idempotent: a second sync updates in place.
        await sync_player_seasons(session, client, season=2025)
    assert result.status == m.CFBDSyncStatus.succeeded
    rows = list((await session.execute(select(m.CFBDPlayerSeason))).scalars())
    assert len(rows) == 1
    row = rows[0]
    assert row.athlete_id == 101
    assert row.stats == {"passing": {"YDS": "2104"}, "rushing": {"CAR": "41"}}
    assert row.usage == {"overall": 0.4}
    assert row.ppa == {"average": {"all": 0.21}, "total": {"all": 80.5}}


def test_current_season_rolls_over_in_march() -> None:
    from datetime import UTC, datetime

    from app.cfbd.roster_sync import current_season

    assert current_season(datetime(2026, 10, 16, tzinfo=UTC)) == 2026
    assert current_season(datetime(2027, 1, 5, tzinfo=UTC)) == 2026
    assert current_season(datetime(2027, 3, 1, tzinfo=UTC)) == 2027


async def test_locked_jersey_is_preserved_without_erasing_duplicate_candidates(
    session: AsyncSession,
) -> None:
    async with _client(_ROSTER) as client:
        await sync_roster(session, client, season=2026)
    players = await _players(session)
    # A coach locks Late Addition at #7/DB and moves Smith's slot aside.
    smith, late = players["Armorion Smith"], players["Late Addition"]
    smith.jersey_number = None
    await session.flush()
    late.jersey_number = 7
    late.metadata_ = {**(late.metadata_ or {}), "staff_locked_fields": ["jersey_number"]}
    await session.commit()

    # Smith appears before the locked player in the vendor rows.
    async with _client(_ROSTER) as client:
        result = await sync_roster(session, client, season=2026)
    assert result.status == m.CFBDSyncStatus.succeeded
    players = await _players(session)
    assert players["Late Addition"].jersey_number == 7
    assert players["Armorion Smith"].jersey_number == 7


async def test_departing_jersey_is_released_for_incoming_player(session: AsyncSession) -> None:
    async with _client(_ROSTER) as client:
        await sync_roster(session, client, season=2025)
    incoming = [
        *[e for e in _ROSTER if e["id"] != 101],
        {"id": 201, "firstName": "New", "lastName": "Starter", "jersey": 7, "position": "QB"},
    ]
    async with _client(incoming) as client:
        await sync_roster(session, client, season=2026)
    players = await _players(session)
    assert players["Khamoni Robinson"].is_active is False
    assert players["New Starter"].jersey_number == 7


async def test_locked_active_status_is_preserved(session: AsyncSession) -> None:
    async with _client(_ROSTER) as client:
        await sync_roster(session, client, season=2025)
    players = await _players(session)
    wr, qb = players["Anthony Simpson"], players["Khamoni Robinson"]
    wr.is_active = False
    wr.metadata_ = {**(wr.metadata_ or {}), "staff_locked_fields": ["is_active"]}
    qb.metadata_ = {**(qb.metadata_ or {}), "staff_locked_fields": ["is_active"]}
    await session.commit()

    # Next season Robinson is gone from CFBD; Simpson is still listed.
    async with _client([e for e in _ROSTER if e["id"] != 101]) as client:
        await sync_roster(session, client, season=2026)
    players = await _players(session)
    assert players["Anthony Simpson"].is_active is False
    assert players["Khamoni Robinson"].is_active is True
