"""Roster seeding utilities for startup bootstrap and ops scripts.

Loads a season roster file (``app/data/roster_<season>.json``, hand-derived
from the official published roster) and upserts ``players`` rows idempotently.

Identity model
--------------
Jersey numbers repeat across sides of the ball on a college roster (two #7s —
a QB and a CB — is normal), so a jersey alone never identifies a player. The
stable identity key is ``metadata->>'roster_key'`` (season + name slug, with
the jersey appended only to break ties between identically named players).
The key deliberately excludes jersey and position: players change both
between seasons and even mid-season, and a key built from them would fork a
new row instead of updating in place. Film-side identity resolution keys on
``(jersey_number, position_group)`` — enforced unique for active players by
``uq_players_jersey_posgroup_active``.

Seeding never deletes: players who leave the roster are deactivated
(``is_active=false``) only when the caller opts in, because tracklets and
profile history reference player rows.

Pre-existing installations: player rows created through the API before this
seeding existed carry no ``roster_key``. The seed pass adopts them instead of
inserting duplicates — matching by normalized name first, then by the
constrained ``(jersey_number, position_group)`` pair — so the partial unique
index can never be violated by a seed run over live data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Player

log = structlog.get_logger(__name__)

DEFAULT_ROSTER_PATH = Path(__file__).parent / "data" / "roster_2026.json"

# Roster-owned metadata keys, replaced wholesale on each seed pass. Keys
# outside this set (e.g. notes added through the API) are preserved.
_ROSTER_METADATA_KEYS = frozenset(
    {
        "class_year",
        "height_in",
        "weight_lb",
        "hometown",
        "high_school",
        "previous_school",
        "roster_season",
        "roster_key",
    }
)


class RosterEntry(BaseModel):
    """One player row from the roster file."""

    first_name: str
    last_name: str
    jersey_number: int | None = None
    position: str | None = None
    position_group: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def roster_key(self) -> str:
        key = self.metadata.get("roster_key")
        if not isinstance(key, str) or not key:
            raise ValueError(f"roster entry {self.first_name} {self.last_name} has no roster_key")
        return key


class RosterFile(BaseModel):
    """The committed roster document."""

    season: str
    source: str
    players: list[RosterEntry]


def load_roster_file(path: Path | None = None) -> RosterFile:
    """Load and validate the roster file, rejecting duplicate identity keys."""
    roster_path = path or DEFAULT_ROSTER_PATH
    roster = RosterFile.model_validate(json.loads(roster_path.read_text(encoding="utf-8")))

    seen_keys: set[str] = set()
    seen_pairs: dict[tuple[int, str], str] = {}
    for entry in roster.players:
        key = entry.roster_key
        if key in seen_keys:
            raise ValueError(f"duplicate roster_key in roster file: {key}")
        seen_keys.add(key)
        # Pre-flight the DB uniqueness rule so a bad file fails with a readable
        # message instead of an IntegrityError mid-transaction.
        if entry.jersey_number is not None and entry.position_group:
            pair = (entry.jersey_number, entry.position_group)
            if pair in seen_pairs:
                raise ValueError(
                    "roster file violates (jersey_number, position_group) uniqueness: "
                    f"#{pair[0]} {pair[1]} is claimed by both {seen_pairs[pair]} and {key}"
                )
            seen_pairs[pair] = key
    return roster


def _merged_metadata(existing: dict[str, Any] | None, entry: RosterEntry) -> dict[str, Any]:
    merged = {k: v for k, v in (existing or {}).items() if k not in _ROSTER_METADATA_KEYS}
    merged.update(entry.metadata)
    return merged


def _apply_entry(row: Player, entry: RosterEntry) -> bool:
    """Copy roster fields onto an existing row; returns True if anything changed."""
    changed = False
    updates: dict[str, Any] = {
        "first_name": entry.first_name,
        "last_name": entry.last_name,
        "jersey_number": entry.jersey_number,
        "position": entry.position,
        "position_group": entry.position_group,
        "is_active": True,
    }
    for attr, value in updates.items():
        if getattr(row, attr) != value:
            setattr(row, attr, value)
            changed = True
    metadata = _merged_metadata(row.metadata_, entry)
    if row.metadata_ != metadata:
        row.metadata_ = metadata
        changed = True
    return changed


async def seed_roster(
    *,
    database_url: str,
    roster_path: Path | None = None,
    deactivate_missing: bool = False,
) -> dict[str, int]:
    """Upsert the roster file into ``players``; returns counts by action."""
    roster = load_roster_file(roster_path)
    file_keys = {entry.roster_key for entry in roster.players}

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stats = {"created": 0, "updated": 0, "unchanged": 0, "deactivated": 0}

    try:
        async with session_factory() as session:
            result = await session.execute(select(Player))
            all_players = list(result.scalars())
            by_key: dict[str, Player] = {}
            unkeyed: list[Player] = []
            for row in all_players:
                key = (row.metadata_ or {}).get("roster_key")
                if isinstance(key, str) and key:
                    by_key[key] = row
                else:
                    unkeyed.append(row)

            def _adopt_existing(entry: RosterEntry) -> Player | None:
                """Match a pre-seeding row so we update instead of colliding."""
                first = entry.first_name.strip().lower()
                last = entry.last_name.strip().lower()
                for row in unkeyed:
                    if (
                        row.first_name.strip().lower() == first
                        and row.last_name.strip().lower() == last
                    ):
                        unkeyed.remove(row)
                        return row
                if entry.jersey_number is not None and entry.position_group:
                    for row in unkeyed:
                        if (
                            row.is_active
                            and row.jersey_number == entry.jersey_number
                            and row.position_group == entry.position_group
                        ):
                            unkeyed.remove(row)
                            return row
                return None

            # Phase 1 of the two-phase update: any matched row whose
            # (jersey, group) pair is about to change gets its jersey nulled
            # and flushed first. Without this, two players legitimately
            # swapping numbers inside one position group would collide with
            # the immediate unique index mid-update and roll back the seed.
            matched: list[tuple[RosterEntry, Player | None]] = []
            for entry in roster.players:
                existing: Player | None = by_key.get(entry.roster_key)
                if existing is None:
                    existing = _adopt_existing(entry)
                    if existing is not None:
                        by_key[entry.roster_key] = existing
                matched.append((entry, existing))

            needs_clear = False
            for entry, existing in matched:
                if existing is None:
                    continue
                old_pair = (existing.jersey_number, existing.position_group)
                new_pair = (entry.jersey_number, entry.position_group)
                if old_pair != new_pair and existing.jersey_number is not None:
                    existing.jersey_number = None
                    needs_clear = True
            if needs_clear:
                await session.flush()

            for entry, existing in matched:
                if existing is None:
                    session.add(
                        Player(
                            first_name=entry.first_name,
                            last_name=entry.last_name,
                            jersey_number=entry.jersey_number,
                            position=entry.position,
                            position_group=entry.position_group,
                            metadata_=_merged_metadata(None, entry),
                            is_active=True,
                        )
                    )
                    stats["created"] += 1
                elif _apply_entry(existing, entry):
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1

            if deactivate_missing:
                for key, row in by_key.items():
                    if (
                        key not in file_keys
                        and row.is_active
                        and row.metadata_ is not None
                        and row.metadata_.get("roster_season") == roster.season
                    ):
                        row.is_active = False
                        stats["deactivated"] += 1

            await session.commit()
    finally:
        await engine.dispose()

    log.info("roster_seed_complete", season=roster.season, **stats)
    return stats
