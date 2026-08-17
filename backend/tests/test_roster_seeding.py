"""Roster seeding tests: file validation (pure) and upsert semantics (real DB).

The upsert tests run against the configured test database (same pattern as
test_jobs_claim / test_cfbd_sync), creating only the tables they need and
cleaning up the rows they add.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import Player
from app.roster_seeding import DEFAULT_ROSTER_PATH, load_roster_file, seed_roster
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TEST_SEASON = "test-9999"


def _entry(
    first: str,
    last: str,
    jersey: int,
    position: str,
    group: str,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "first_name": first,
        "last_name": last,
        "jersey_number": jersey,
        "position": position,
        "position_group": group,
        "metadata": {
            "roster_season": _TEST_SEASON,
            # Stable across jersey/position changes — matches the real file's
            # season + name-slug convention.
            "roster_key": f"{_TEST_SEASON}-{last}-{first}".lower(),
            **metadata,
        },
    }


def _write_roster(path: Path, players: list[dict[str, Any]]) -> Path:
    path.write_text(
        json.dumps({"season": _TEST_SEASON, "source": "unit test", "players": players}),
        encoding="utf-8",
    )
    return path


# ── File validation (no DB) ──────────────────────────────────────────────────


def test_committed_roster_file_is_valid() -> None:
    roster = load_roster_file()
    assert roster.season == "2026"
    assert len(roster.players) >= 90
    # Every entry carries the identity key the upsert matches on.
    keys = {p.roster_key for p in roster.players}
    assert len(keys) == len(roster.players)


def test_committed_roster_has_cross_side_jersey_duplicates() -> None:
    """The duplicate-jersey reality this design exists for is present."""
    roster = load_roster_file(DEFAULT_ROSTER_PATH)
    by_jersey: dict[int, set[str]] = {}
    for p in roster.players:
        if p.jersey_number is not None and p.position_group:
            by_jersey.setdefault(p.jersey_number, set()).add(p.position_group)
    assert any(len(groups) > 1 for groups in by_jersey.values())


def test_duplicate_roster_key_rejected(tmp_path: Path) -> None:
    entry = _entry("Cam", "Jones", 1, "CB", "DB")
    path = _write_roster(tmp_path / "r.json", [entry, entry])
    with pytest.raises(ValueError, match="duplicate roster_key"):
        load_roster_file(path)


def test_duplicate_jersey_within_group_rejected(tmp_path: Path) -> None:
    players = [
        _entry("Cam", "Jones", 1, "CB", "DB"),
        _entry("Rico", "Bond", 1, "S", "DB"),
    ]
    path = _write_roster(tmp_path / "r.json", players)
    with pytest.raises(ValueError, match="jersey_number, position_group"):
        load_roster_file(path)


def test_duplicate_jersey_across_groups_accepted(tmp_path: Path) -> None:
    players = [
        _entry("Cam", "Jones", 1, "CB", "DB"),
        _entry("Rico", "Bond", 1, "WR", "Skill"),
    ]
    path = _write_roster(tmp_path / "r.json", players)
    assert len(load_roster_file(path).players) == 2


def test_missing_roster_key_rejected(tmp_path: Path) -> None:
    entry = _entry("Cam", "Jones", 1, "CB", "DB")
    del entry["metadata"]["roster_key"]
    path = _write_roster(tmp_path / "r.json", [entry])
    with pytest.raises(ValueError, match="roster_key"):
        load_roster_file(path)


# ── Upsert semantics (real DB) ───────────────────────────────────────────────


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        existing: set[str] = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    players_table = Base.metadata.tables["players"]
    users_table = Base.metadata.tables["users"]
    created = [t for t in (users_table, players_table) if t.name not in existing]
    if created:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=created))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                delete(Player).where(Player.metadata_["roster_season"].astext == _TEST_SEASON)
            )
        if created:
            async with engine.begin() as conn:
                for table in reversed(created):
                    await conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
        await engine.dispose()


async def _seeded_players(maker: async_sessionmaker[AsyncSession]) -> list[Player]:
    async with maker() as session:
        result = await session.execute(
            select(Player).where(Player.metadata_["roster_season"].astext == _TEST_SEASON)
        )
        return list(result.scalars())


async def test_seed_is_idempotent(db: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    path = _write_roster(
        tmp_path / "r.json",
        [
            _entry("Cam", "Jones", 1, "CB", "DB"),
            _entry("Rico", "Bond", 1, "WR", "Skill"),
            _entry("Khamoni", "Robinson", 7, "QB", "QB"),
        ],
    )
    url = get_settings().database_url

    first = await seed_roster(database_url=url, roster_path=path)
    assert first == {"created": 3, "updated": 0, "unchanged": 0, "deactivated": 0}

    second = await seed_roster(database_url=url, roster_path=path)
    assert second == {"created": 0, "updated": 0, "unchanged": 3, "deactivated": 0}

    rows = await _seeded_players(db)
    assert len(rows) == 3
    # Both #1s exist — cross-side duplicate jerseys are first-class.
    ones = sorted(r.position_group or "" for r in rows if r.jersey_number == 1)
    assert ones == ["DB", "Skill"]


async def test_seed_updates_in_place_and_preserves_foreign_metadata(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    url = get_settings().database_url
    path = _write_roster(
        tmp_path / "r.json", [_entry("Cam", "Jones", 1, "CB", "DB", weight_lb=190)]
    )
    await seed_roster(database_url=url, roster_path=path)

    # A non-roster metadata key added through the API must survive re-seeding.
    async with db() as session:
        row = (await session.execute(select(Player))).scalars().one()
        row.metadata_ = {**(row.metadata_ or {}), "coach_note": "keep"}
        await session.commit()
        player_id = row.id

    _write_roster(tmp_path / "r.json", [_entry("Cam", "Jones", 24, "CB", "DB", weight_lb=195)])
    stats = await seed_roster(database_url=url, roster_path=path)
    assert stats["updated"] == 1

    async with db() as session:
        row = (await session.execute(select(Player))).scalars().one()
        assert row.id == player_id  # same row, not a new one
        assert row.jersey_number == 24
        assert row.metadata_ is not None
        assert row.metadata_["weight_lb"] == 195
        assert row.metadata_["coach_note"] == "keep"


async def test_deactivate_missing_is_opt_in_and_scoped(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    url = get_settings().database_url
    path = _write_roster(
        tmp_path / "r.json",
        [_entry("Cam", "Jones", 1, "CB", "DB"), _entry("Rico", "Bond", 1, "WR", "Skill")],
    )
    await seed_roster(database_url=url, roster_path=path)

    smaller = _write_roster(tmp_path / "r2.json", [_entry("Cam", "Jones", 1, "CB", "DB")])

    # Default: nobody is deactivated.
    stats = await seed_roster(database_url=url, roster_path=smaller)
    assert stats["deactivated"] == 0

    stats = await seed_roster(database_url=url, roster_path=smaller, deactivate_missing=True)
    assert stats["deactivated"] == 1

    rows = {(r.first_name, r.is_active) for r in await _seeded_players(db)}
    assert rows == {("Cam", True), ("Rico", False)}

    # Re-seeding the full file reactivates the returning player.
    stats = await seed_roster(database_url=url, roster_path=path)
    assert stats["updated"] == 1
    assert all(r.is_active for r in await _seeded_players(db))
