"""CFBD → roster and per-player season data.

``sync_roster`` makes ``players`` match Toledo's CFBD roster for a season:
it matches by CFBD athlete id (``metadata.cfbd_athlete_id``), adopts rows
created before the sync by name, and never overwrites a field a coach edited
in the roster editor (``metadata.staff_locked_fields``). Practice numbers and
positions often differ from the official roster, so those edits must stick.

``sync_player_seasons`` caches each player's season box-score stats, usage
and PPA in ``cfbd_player_seasons`` for the player profile.

Both are idempotent and record a ``cfbd_sync_runs`` row like the other syncs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cfbd.client import CFBDClient
from app.cfbd.errors import CFBDError
from app.cfbd.models import CFBDPlayerSeason, CFBDSyncRun, CFBDSyncStatus
from app.cfbd.sync import DEFAULT_TEAM, EndpointSyncResult, _record_run, _upsert
from app.models import Player

log = structlog.get_logger("app.cfbd.roster_sync")

#: CFBD position code → the coaching position group used across the app.
POSITION_GROUPS: dict[str, str] = {
    "QB": "QB",
    "RB": "Skill",
    "FB": "Skill",
    "WR": "Skill",
    "TE": "Skill",
    "ATH": "Skill",
    "OL": "OL",
    "OT": "OL",
    "OG": "OL",
    "G": "OL",
    "T": "OL",
    "C": "OL",
    "DL": "DL",
    "DE": "DL",
    "DT": "DL",
    "NT": "DL",
    "EDGE": "DL",
    "LB": "LB",
    "ILB": "LB",
    "OLB": "LB",
    "DB": "DB",
    "CB": "DB",
    "S": "DB",
    "FS": "DB",
    "SS": "DB",
    "NB": "DB",
    "K": "ST",
    "PK": "ST",
    "P": "ST",
    "LS": "ST",
}

_CLASS_YEARS = {1: "Fr.", 2: "So.", 3: "Jr.", 4: "Sr.", 5: "Gr.", 6: "Gr."}

#: Player fields a coach can edit; a locked one is never overwritten by sync.
SYNCED_FIELDS = ("first_name", "last_name", "jersey_number", "position", "position_group")


def position_group(position: str | None) -> str | None:
    if not position:
        return None
    return POSITION_GROUPS.get(position.strip().upper())


def _hometown(entry: dict[str, Any]) -> str | None:
    parts = [entry.get("homeCity"), entry.get("homeState")]
    text = ", ".join(str(p) for p in parts if p)
    country = entry.get("homeCountry")
    if country and country not in {"USA", "US", "United States"}:
        text = f"{text}, {country}" if text else str(country)
    return text or None


def roster_fields(entry: dict[str, Any], season: int) -> dict[str, Any]:
    """Map one CFBD roster row to player columns plus metadata."""
    athlete_id = int(entry["id"])
    position = (entry.get("position") or "").strip().upper() or None
    jersey = entry.get("jersey")
    metadata: dict[str, Any] = {
        "cfbd_athlete_id": str(athlete_id),
        "roster_key": f"cfbd-{athlete_id}",
        "roster_season": str(season),
        "roster_source": "CollegeFootballData",
        "class_year": _CLASS_YEARS.get(entry.get("year") or 0),
        "height_in": entry.get("height"),
        "weight_lb": entry.get("weight"),
        "hometown": _hometown(entry),
    }
    return {
        "first_name": (entry.get("firstName") or "").strip(),
        "last_name": (entry.get("lastName") or "").strip(),
        "jersey_number": int(jersey) if isinstance(jersey, int | float) else None,
        "position": position,
        "position_group": position_group(position),
        "metadata": {k: v for k, v in metadata.items() if v is not None},
    }


def _locked(player: Player) -> set[str]:
    return set((player.metadata_ or {}).get("staff_locked_fields") or [])


def _name_key(first: str, last: str) -> tuple[str, str]:
    return (first.strip().lower(), last.strip().lower())


async def sync_roster(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    team: str = DEFAULT_TEAM,
) -> EndpointSyncResult:
    endpoint = "/roster"
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    try:
        roster = [e for e in await client.get_roster(team=team, year=season) if e.get("id")]
        result.rows_read = len(roster)
        stats = await _apply_roster(session, roster, season)
        await session.commit()
        result.rows_written = stats["created"] + stats["updated"]
        result.status = CFBDSyncStatus.succeeded
        log.info("cfbd.roster.synced", season=season, **stats)
    except CFBDError as exc:
        await session.rollback()
        result.status = CFBDSyncStatus.failed
        result.error = str(exc)
        log.warning("cfbd.sync.failed", endpoint=endpoint, error_type=type(exc).__name__)
    await _record_run(
        session,
        endpoint=endpoint,
        params={"team": team, "year": season},
        season=season,
        season_type=None,
        result=result,
    )
    return result


async def _apply_roster(
    session: AsyncSession, roster: list[dict[str, Any]], season: int
) -> dict[str, int]:
    players = list((await session.execute(select(Player).with_for_update())).scalars())
    by_athlete: dict[str, Player] = {}
    by_name: dict[tuple[str, str], Player] = {}
    for player in players:
        athlete = (player.metadata_ or {}).get("cfbd_athlete_id")
        if athlete:
            by_athlete[str(athlete)] = player
        else:
            by_name.setdefault(_name_key(player.first_name, player.last_name), player)

    stats = {"created": 0, "updated": 0, "unchanged": 0, "deactivated": 0, "jersey_conflicts": 0}
    planned: list[tuple[dict[str, Any], Player | None]] = []
    for entry in roster:
        fields = roster_fields(entry, season)
        if not fields["first_name"] and not fields["last_name"]:
            continue
        athlete = fields["metadata"]["cfbd_athlete_id"]
        existing = by_athlete.get(athlete) or by_name.pop(
            _name_key(fields["first_name"], fields["last_name"]), None
        )
        planned.append((fields, existing))

    # The partial unique index on active (jersey, position group) is checked
    # row by row, so clear the numbers that are about to move first: two
    # players swapping numbers must not collide halfway through the sync.
    synced = {id(p) for _, p in planned if p is not None}
    taken: set[tuple[int, str]] = {
        (p.jersey_number, p.position_group)
        for p in players
        if id(p) not in synced
        and p.is_active
        and p.jersey_number is not None
        and p.position_group is not None
    }
    original_jersey: dict[int, int | None] = {}
    for _fields, existing in planned:
        if existing is not None and "jersey_number" not in _locked(existing):
            original_jersey[id(existing)] = existing.jersey_number
            existing.jersey_number = None
    await session.flush()

    for fields, existing in planned:
        locked = _locked(existing) if existing is not None else set()
        target = {k: fields[k] for k in SYNCED_FIELDS if k not in locked}
        if existing is not None and "jersey_number" in locked:
            jersey, group = (
                existing.jersey_number,
                target.get("position_group", existing.position_group),
            )
        else:
            jersey, group = (
                target.get("jersey_number"),
                target.get("position_group", existing.position_group if existing else None),
            )
        if jersey is not None and group is not None:
            if (jersey, group) in taken:
                # Two athletes share a number within a group (or a coach
                # assigned it): keep the player, leave the number blank.
                stats["jersey_conflicts"] += 1
                if "jersey_number" in target:
                    target["jersey_number"] = None
            else:
                taken.add((jersey, group))

        if existing is None:
            session.add(
                Player(
                    first_name=fields["first_name"],
                    last_name=fields["last_name"],
                    jersey_number=target.get("jersey_number"),
                    position=fields["position"],
                    position_group=fields["position_group"],
                    metadata_=fields["metadata"],
                    is_active=True,
                )
            )
            stats["created"] += 1
            continue

        changed = False
        for name, value in target.items():
            before = (
                original_jersey.get(id(existing))
                if name == "jersey_number"
                else getattr(existing, name)
            )
            if getattr(existing, name) != value:
                setattr(existing, name, value)
            changed = changed or before != value
        metadata = {**(existing.metadata_ or {}), **fields["metadata"]}
        if metadata != existing.metadata_:
            existing.metadata_ = metadata
            changed = True
        if not existing.is_active:
            existing.is_active = True
            changed = True
        stats["updated" if changed else "unchanged"] += 1

    # Athletes who left the program: a previous CFBD season's player not on
    # this season's roster. Coach-created players (no athlete id) are kept.
    current = {fields["metadata"]["cfbd_athlete_id"] for fields, _ in planned}
    for athlete, player in by_athlete.items():
        meta = player.metadata_ or {}
        if (
            athlete not in current
            and player.is_active
            and str(meta.get("roster_season")) < str(season)
        ):
            player.is_active = False
            stats["deactivated"] += 1
    await session.flush()
    return stats


def _merge_player_rows(
    stat_rows: list[dict[str, Any]],
    usage_rows: list[dict[str, Any]],
    ppa_rows: list[dict[str, Any]],
    season: int,
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}

    def row_for(athlete: Any, source: dict[str, Any]) -> dict[str, Any] | None:
        try:
            key = int(athlete)
        except (TypeError, ValueError):
            return None
        row = merged.setdefault(
            key,
            {"season": season, "athlete_id": key, "stats": None, "usage": None, "ppa": None},
        )
        row["team"] = source.get("team") or row.get("team")
        row["name"] = source.get("player") or source.get("name") or row.get("name")
        row["position"] = source.get("position") or row.get("position")
        return row

    stats_by_player: dict[int, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    for item in stat_rows:
        row = row_for(item.get("playerId"), item)
        if row is None or not item.get("category") or not item.get("statType"):
            continue
        stats_by_player[row["athlete_id"]][str(item["category"])][str(item["statType"])] = item.get(
            "stat"
        )
    for athlete, categories in stats_by_player.items():
        merged[athlete]["stats"] = {k: dict(v) for k, v in categories.items()}
    for item in usage_rows:
        row = row_for(item.get("id"), item)
        if row is not None:
            row["usage"] = item.get("usage")
    for item in ppa_rows:
        row = row_for(item.get("id"), item)
        if row is not None:
            row["ppa"] = {"average": item.get("averagePPA"), "total": item.get("totalPPA")}
    return list(merged.values())


async def sync_player_seasons(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    team: str = DEFAULT_TEAM,
) -> EndpointSyncResult:
    endpoint = "/stats/player/season"
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    try:
        stat_rows = await client.get_player_season_stats(team=team, year=season)
        usage_rows = await client.get_player_usage(team=team, year=season)
        ppa_rows = await client.get_player_ppa(team=team, year=season)
        rows = _merge_player_rows(stat_rows, usage_rows, ppa_rows, season)
        result.rows_read = len(stat_rows) + len(usage_rows) + len(ppa_rows)
        result.rows_written = await _upsert(
            session, CFBDPlayerSeason, rows, conflict_cols=["season", "athlete_id"]
        )
        await session.commit()
        result.status = CFBDSyncStatus.succeeded
    except CFBDError as exc:
        await session.rollback()
        result.status = CFBDSyncStatus.failed
        result.error = str(exc)
        log.warning("cfbd.sync.failed", endpoint=endpoint, error_type=type(exc).__name__)
    await _record_run(
        session,
        endpoint=endpoint,
        params={"team": team, "year": season},
        season=season,
        season_type=None,
        result=result,
    )
    return result


async def sync_roster_and_stats(
    session: AsyncSession, client: CFBDClient, *, season: int, team: str = DEFAULT_TEAM
) -> EndpointSyncResult:
    """Roster for ``season`` plus season stats for it and the year before.

    Early in a season the current year has few stats, so returning players'
    previous season is what a profile needs. One combined result for the UI.
    """
    roster = await sync_roster(session, client, season=season, team=team)
    combined = EndpointSyncResult(
        endpoint="/roster", status=roster.status, rows_read=roster.rows_read
    )
    combined.rows_written = roster.rows_written
    for year in (season, season - 1):
        stats = await sync_player_seasons(session, client, season=year, team=team)
        combined.rows_read += stats.rows_read
        combined.rows_written += stats.rows_written
        if stats.status != CFBDSyncStatus.succeeded:
            combined.status = CFBDSyncStatus.failed
            combined.error = stats.error
    return combined


#: How often the nightly tick refreshes the roster and player season stats.
WEEKLY_REFRESH = timedelta(days=7)


def current_season(now: datetime) -> int:
    """The football season a date belongs to (January/February bowl and
    off-season dates still belong to the previous fall's season)."""
    return now.year if now.month >= 3 else now.year - 1


async def refresh_roster_if_due(
    session: AsyncSession, now: datetime | None = None
) -> dict[str, Any] | None:
    """Weekly roster + player-stats refresh, called after the nightly tick.

    No-op without a CFBD key or when a successful roster sync ran in the last
    week. Runs in its own session (sync steps commit as they go), never
    inside the tick's advisory-locked transaction.
    """
    from app.config import get_settings

    if not get_settings().cfbd_api_key.strip():
        return None
    now = now or datetime.now(UTC)
    last = (
        await session.execute(
            select(func.max(CFBDSyncRun.finished_at)).where(
                CFBDSyncRun.endpoint == "/roster",
                CFBDSyncRun.status == CFBDSyncStatus.succeeded,
            )
        )
    ).scalar_one()
    if last is not None and now - last < WEEKLY_REFRESH:
        return None
    season = current_season(now)
    async with CFBDClient.from_settings(timeout=20.0, max_retries=2) as client:
        result = await sync_roster_and_stats(session, client, season=season)
    return {"season": season, "status": result.status.value, "rows_written": result.rows_written}
