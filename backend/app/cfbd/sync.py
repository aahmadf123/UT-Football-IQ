"""Idempotent CFBD → Postgres ingestion for Toledo + MAC (Issue #162).

Each endpoint sync upserts into its cache table (so re-running is safe and
non-destructive) and records a :class:`~app.cfbd.models.CFBDSyncRun` row with the
endpoint, params, timestamps, row counts, status, and a safe error summary.
Endpoint syncs are independent: if one fails the others still run and previously
cached data stays queryable, so analytics keep working when CFBD is degraded.

No vendor key is ever written here — the key lives only on the in-memory
``CFBDClient`` and is never part of a sync-run record or log line.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cfbd.client import CFBDClient
from app.cfbd.errors import CFBDError
from app.cfbd.models import (
    CFBDDrive,
    CFBDGame,
    CFBDPlay,
    CFBDSyncRun,
    CFBDSyncStatus,
    CFBDTeam,
    CFBDTeamGameStat,
    CFBDWinProbability,
)

log = structlog.get_logger("app.cfbd.sync")

DEFAULT_CONFERENCE = "MAC"
DEFAULT_TEAM = "Toledo"
DEFAULT_SEASON_TYPE = "regular"
# CFBD's /plays endpoint requires a week; regular-season weeks plus a buffer for
# conference championship week. Postseason is synced separately when requested.
DEFAULT_WEEKS: tuple[int, ...] = tuple(range(1, 16))
# Stay safely below asyncpg's 32,767 bind-parameter ceiling per statement.
_MAX_BIND_PARAMS = 30_000


@dataclass
class EndpointSyncResult:
    """Outcome of a single endpoint sync, mirrored into ``cfbd_sync_runs``."""

    endpoint: str
    status: CFBDSyncStatus
    rows_read: int = 0
    rows_written: int = 0
    error: str | None = None


@dataclass
class SeasonSyncSummary:
    """Aggregate result of syncing a full season for Toledo + MAC."""

    season: int
    season_type: str
    results: dict[str, EndpointSyncResult] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "season_type": self.season_type,
            "results": {k: asdict(v) for k, v in self.results.items()},
        }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _upsert(
    session: AsyncSession,
    table: Any,
    rows: list[dict[str, Any]],
    *,
    conflict_cols: Sequence[str],
) -> int:
    """Upsert ``rows`` into ``table`` on ``conflict_cols``; return row count.

    All non-conflict value columns are refreshed on conflict, and ``updated_at``
    is bumped, so a re-sync overwrites stale fields without creating duplicates.
    """
    if not rows:
        return 0
    # asyncpg caps one statement at 32,767 bind parameters; a conference
    # week of plays exceeds that, so split the multi-row VALUES into batches.
    # Python-side column defaults also become per-row parameters, so size
    # batches by the full table width rather than the supplied keys.
    columns = getattr(table, "__table__", table).columns
    width = max(len(columns), *(len(row) for row in rows))
    batch_size = max(1, _MAX_BIND_PARAMS // width)
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        stmt = pg_insert(table).values(batch)
        value_keys = set(batch[0].keys())
        update_set: dict[str, Any] = {
            key: stmt.excluded[key] for key in value_keys if key not in conflict_cols
        }
        update_set["updated_at"] = func.now()
        stmt = stmt.on_conflict_do_update(index_elements=list(conflict_cols), set_=update_set)
        await session.execute(stmt)
    return len(rows)


async def _record_run(
    session: AsyncSession,
    *,
    endpoint: str,
    params: dict[str, Any],
    season: int | None,
    season_type: str | None,
    result: EndpointSyncResult,
) -> None:
    """Persist a durable ``cfbd_sync_runs`` row for one endpoint sync."""
    run = CFBDSyncRun(
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        status=result.status,
        finished_at=datetime.now().astimezone(),
        rows_read=result.rows_read,
        rows_written=result.rows_written,
        error_summary=result.error,
    )
    session.add(run)
    await session.commit()


# ── Per-endpoint syncs ──────────────────────────────────────────────────────


async def sync_teams(
    session: AsyncSession,
    client: CFBDClient,
    *,
    conference: str = DEFAULT_CONFERENCE,
    season: int | None = None,
    season_type: str | None = None,
) -> EndpointSyncResult:
    endpoint = "/teams"
    params = {"conference": conference}
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    try:
        teams = await client.get_teams(conference=conference)
        rows = [
            {
                "cfbd_team_id": t.id,
                "school": t.school,
                "mascot": t.mascot,
                "abbreviation": t.abbreviation,
                "conference": t.conference,
                "division": t.division,
                "classification": t.classification,
                "color": t.color,
                "alt_color": t.alt_color,
                "logos": t.logos,
            }
            for t in teams
        ]
        written = await _upsert(session, CFBDTeam, rows, conflict_cols=["cfbd_team_id"])
        result.rows_read = len(teams)
        result.rows_written = written
        result.status = CFBDSyncStatus.succeeded
    except CFBDError as exc:
        result.status = CFBDSyncStatus.failed
        result.error = str(exc)
        log.warning("cfbd.sync.failed", endpoint=endpoint, error_type=type(exc).__name__)
    await _record_run(
        session,
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        result=result,
    )
    return result


async def sync_games(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    season_type: str = DEFAULT_SEASON_TYPE,
    conference: str = DEFAULT_CONFERENCE,
    team: str = DEFAULT_TEAM,
) -> EndpointSyncResult:
    endpoint = "/games"
    params = {"year": season, "conference": conference, "team": team, "seasonType": season_type}
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    try:
        # Conference slate plus the team's full schedule (catches non-conference
        # opponents the conference filter misses), de-duplicated by source id.
        conf_games = await client.get_games(
            year=season, conference=conference, season_type=season_type
        )
        team_games = await client.get_games(year=season, team=team, season_type=season_type)
        by_id: dict[int, Any] = {}
        for g in [*conf_games, *team_games]:
            by_id[g.id] = g
        rows = [
            {
                "cfbd_game_id": g.id,
                "season": g.season,
                "week": g.week,
                "season_type": g.season_type or season_type,
                "start_date": _parse_dt(g.start_date),
                "neutral_site": g.neutral_site,
                "conference_game": g.conference_game,
                "venue": g.venue,
                "venue_id": g.venue_id,
                "home_id": g.home_id,
                "home_team": g.home_team,
                "home_conference": g.home_conference,
                "home_points": g.home_points,
                "away_id": g.away_id,
                "away_team": g.away_team,
                "away_conference": g.away_conference,
                "away_points": g.away_points,
                "completed": g.completed,
            }
            for g in by_id.values()
        ]
        written = await _upsert(session, CFBDGame, rows, conflict_cols=["cfbd_game_id"])
        result.rows_read = len(by_id)
        result.rows_written = written
        result.status = CFBDSyncStatus.succeeded
    except CFBDError as exc:
        result.status = CFBDSyncStatus.failed
        result.error = str(exc)
        log.warning("cfbd.sync.failed", endpoint=endpoint, error_type=type(exc).__name__)
    await _record_run(
        session,
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        result=result,
    )
    return result


async def sync_drives(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    season_type: str = DEFAULT_SEASON_TYPE,
    conference: str = DEFAULT_CONFERENCE,
) -> EndpointSyncResult:
    endpoint = "/drives"
    params = {"year": season, "conference": conference, "seasonType": season_type}
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    try:
        drives = await client.get_drives(
            year=season, conference=conference, season_type=season_type
        )
        rows = [
            {
                "cfbd_drive_id": d.id,
                "cfbd_game_id": d.game_id,
                "season": season,
                "offense": d.offense,
                "offense_conference": d.offense_conference,
                "defense": d.defense,
                "defense_conference": d.defense_conference,
                "drive_number": d.drive_number,
                "scoring": d.scoring,
                "start_period": d.start_period,
                "end_period": d.end_period,
                "plays": d.plays,
                "yards": d.yards,
                "drive_result": d.drive_result,
            }
            for d in drives
        ]
        written = await _upsert(session, CFBDDrive, rows, conflict_cols=["cfbd_drive_id"])
        result.rows_read = len(drives)
        result.rows_written = written
        result.status = CFBDSyncStatus.succeeded
    except CFBDError as exc:
        result.status = CFBDSyncStatus.failed
        result.error = str(exc)
        log.warning("cfbd.sync.failed", endpoint=endpoint, error_type=type(exc).__name__)
    await _record_run(
        session,
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        result=result,
    )
    return result


async def sync_plays(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    season_type: str = DEFAULT_SEASON_TYPE,
    conference: str = DEFAULT_CONFERENCE,
    weeks: Iterable[int] = DEFAULT_WEEKS,
) -> EndpointSyncResult:
    endpoint = "/plays"
    week_list = list(weeks)
    params = {
        "year": season,
        "conference": conference,
        "seasonType": season_type,
        "weeks": week_list,
    }
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    read = 0
    written = 0
    failures = 0
    errors: list[str] = []
    for week in week_list:
        try:
            plays = await client.get_plays(
                year=season, week=week, conference=conference, season_type=season_type
            )
        except CFBDError as exc:
            failures += 1
            errors.append(f"week={week}: {exc}")
            log.warning(
                "cfbd.sync.partial",
                endpoint=endpoint,
                week=week,
                error_type=type(exc).__name__,
            )
            continue
        rows = [
            {
                "cfbd_play_id": p.id,
                "cfbd_game_id": p.game_id,
                "cfbd_drive_id": p.drive_id,
                "season": season,
                "week": week,
                "season_type": season_type,
                "offense": p.offense,
                "offense_conference": p.offense_conference,
                "defense": p.defense,
                "defense_conference": p.defense_conference,
                "home": p.home,
                "away": p.away,
                "period": p.period,
                "yard_line": p.yard_line,
                "down": p.down,
                "distance": p.distance,
                "yards_gained": p.yards_gained,
                "play_type": p.play_type,
                "play_text": p.play_text,
                "ppa": p.ppa,
                "scoring": p.scoring,
            }
            for p in plays
        ]
        written += await _upsert(session, CFBDPlay, rows, conflict_cols=["cfbd_play_id"])
        read += len(plays)
    result.rows_read = read
    result.rows_written = written
    result.status = _aggregate_status(failures, len(week_list))
    result.error = "; ".join(errors) if errors else None
    await _record_run(
        session,
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        result=result,
    )
    return result


async def sync_team_game_stats(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    season_type: str = DEFAULT_SEASON_TYPE,
    conference: str = DEFAULT_CONFERENCE,
) -> EndpointSyncResult:
    endpoint = "/games/teams"
    params = {"year": season, "conference": conference, "seasonType": season_type}
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)
    try:
        games = await client.get_team_game_stats(
            year=season, conference=conference, season_type=season_type
        )
        rows: list[dict[str, Any]] = []
        read = 0
        for game in games:
            schools = [t.school for t in game.teams if t.school]
            for t in game.teams:
                if not t.school:
                    continue
                read += 1
                opponent = next((s for s in schools if s != t.school), None)
                rows.append(
                    {
                        "cfbd_game_id": game.id,
                        "season": season,
                        "season_type": season_type,
                        "team": t.school,
                        "team_id": t.team_id,
                        "conference": t.conference,
                        "opponent": opponent,
                        "home_away": t.home_away,
                        "points": t.points,
                        "stats": [s.model_dump() for s in t.stats],
                    }
                )
        written = await _upsert(
            session, CFBDTeamGameStat, rows, conflict_cols=["cfbd_game_id", "team"]
        )
        result.rows_read = read
        result.rows_written = written
        result.status = CFBDSyncStatus.succeeded
    except CFBDError as exc:
        result.status = CFBDSyncStatus.failed
        result.error = str(exc)
        log.warning("cfbd.sync.failed", endpoint=endpoint, error_type=type(exc).__name__)
    await _record_run(
        session,
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        result=result,
    )
    return result


async def sync_win_probability(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    season_type: str = DEFAULT_SEASON_TYPE,
    team: str = DEFAULT_TEAM,
) -> EndpointSyncResult:
    """Sync per-play win probability for every cached game involving ``team``.

    Bounded to the team's games (already in ``cfbd_games``) so the per-game
    ``/metrics/wp`` calls stay to ~a dozen for a season.
    """
    endpoint = "/metrics/wp"
    params = {"season": season, "team": team, "seasonType": season_type}
    result = EndpointSyncResult(endpoint=endpoint, status=CFBDSyncStatus.running)

    game_ids = (
        (
            await session.execute(
                select(CFBDGame.cfbd_game_id).where(
                    CFBDGame.season == season,
                    CFBDGame.season_type == season_type,
                    ((CFBDGame.home_team == team) | (CFBDGame.away_team == team)),
                )
            )
        )
        .scalars()
        .all()
    )

    read = 0
    written = 0
    failures = 0
    errors: list[str] = []
    for game_id in game_ids:
        try:
            wps = await client.get_win_probability(game_id=game_id)
        except CFBDError as exc:
            failures += 1
            errors.append(f"game_id={game_id}: {exc}")
            log.warning(
                "cfbd.sync.partial",
                endpoint=endpoint,
                game_id=game_id,
                error_type=type(exc).__name__,
            )
            continue
        rows = [
            {
                "cfbd_game_id": w.game_id,
                "play_id": w.play_id,
                "play_number": w.play_number,
                "home": w.home,
                "away": w.away,
                "home_id": w.home_id,
                "away_id": w.away_id,
                "spread": w.spread,
                "home_ball": w.home_ball,
                "home_score": w.home_score,
                "away_score": w.away_score,
                "time_remaining": w.time_remaining,
                "yard_line": w.yard_line,
                "down": w.down,
                "distance": w.distance,
                "home_win_prob": w.home_win_prob,
                "play_text": w.play_text,
            }
            for w in wps
            if w.play_number is not None
        ]
        written += await _upsert(
            session,
            CFBDWinProbability,
            rows,
            conflict_cols=["cfbd_game_id", "play_number"],
        )
        read += len(rows)
    result.rows_read = read
    result.rows_written = written
    result.status = _aggregate_status(failures, len(game_ids))
    result.error = "; ".join(errors) if errors else None
    await _record_run(
        session,
        endpoint=endpoint,
        params=params,
        season=season,
        season_type=season_type,
        result=result,
    )
    return result


def _aggregate_status(failures: int, total: int) -> CFBDSyncStatus:
    if failures == 0:
        return CFBDSyncStatus.succeeded
    if failures >= total and total > 0:
        return CFBDSyncStatus.failed
    return CFBDSyncStatus.partial


# ── Orchestration ────────────────────────────────────────────────────────────


async def sync_season(
    session: AsyncSession,
    client: CFBDClient,
    *,
    season: int,
    season_type: str = DEFAULT_SEASON_TYPE,
    conference: str = DEFAULT_CONFERENCE,
    team: str = DEFAULT_TEAM,
    weeks: Iterable[int] = DEFAULT_WEEKS,
) -> SeasonSyncSummary:
    """Sync Toledo + MAC data for one season into the Postgres cache.

    Endpoints are synced in order so win probability can reference the games
    cached earlier in the same run. Each endpoint is independent: a failure is
    recorded in ``cfbd_sync_runs`` and does not abort the rest of the run.
    """
    summary = SeasonSyncSummary(season=season, season_type=season_type)
    summary.results["teams"] = await sync_teams(
        session, client, conference=conference, season=season, season_type=season_type
    )
    summary.results["games"] = await sync_games(
        session,
        client,
        season=season,
        season_type=season_type,
        conference=conference,
        team=team,
    )
    summary.results["drives"] = await sync_drives(
        session, client, season=season, season_type=season_type, conference=conference
    )
    summary.results["plays"] = await sync_plays(
        session,
        client,
        season=season,
        season_type=season_type,
        conference=conference,
        weeks=weeks,
    )
    summary.results["team_game_stats"] = await sync_team_game_stats(
        session, client, season=season, season_type=season_type, conference=conference
    )
    summary.results["win_probability"] = await sync_win_probability(
        session, client, season=season, season_type=season_type, team=team
    )
    log.info(
        "cfbd.sync.season.complete",
        season=season,
        season_type=season_type,
        results={k: v.status.value for k, v in summary.results.items()},
    )
    return summary


async def run_season_sync(
    season: int,
    *,
    season_type: str = DEFAULT_SEASON_TYPE,
    conference: str = DEFAULT_CONFERENCE,
    team: str = DEFAULT_TEAM,
) -> SeasonSyncSummary:
    """Build a client + session from settings and sync one season.

    Raises :class:`~app.cfbd.errors.CFBDConfigError` if ``CFBD_API_KEY`` is
    unset (a backend-only failure). The caller owns reporting; this never
    surfaces the key.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session, CFBDClient.from_settings() as client:
        return await sync_season(
            session,
            client,
            season=season,
            season_type=season_type,
            conference=conference,
            team=team,
        )
