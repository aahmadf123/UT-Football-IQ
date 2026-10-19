"""CFBD analytics router — cached Toledo/MAC college-football data (Issue #163).

Exposes CollegeFootballData.com data that has been cached in Postgres by the
backend-only CFBD client / ingestion (Issues #161/#162). **Every endpoint reads
cached tables only** — there is no live vendor call in the request path and the
CFBD API key is never touched here, so it can never leak to the frontend, logs,
or coach-visible errors.

Each response carries a ``cache`` block (:class:`CfbdCacheMeta`) derived from
``cfbd_sync_runs`` so the UI can render the *empty cache*, *stale cache*, and
*CFBD unavailable* states honestly instead of presenting cached/empty data as
live (Issue #96 no-mock-data rule).

Access is gated by the ``cfbd_analytics:read`` policy (coaching staff only). The
cross-conference MAC benchmark additionally applies workload gating because it
aggregates across the whole conference.

Endpoints (prefix ``/api/cfbd``):
    GET /teams/toledo
    GET /teams/toledo/schedule
    GET /games/{game_id}/plays
    GET /games/{game_id}/drives
    GET /games/{game_id}/win-probability
    GET /mac/benchmark
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cfbd.models import CFBDPlayerSeason
from app.config import get_settings
from app.database import get_db
from app.deps import require_admin, require_coach_or_above
from app.governance import Action, Resource, require_policy
from app.models import (
    CfbdDrive,
    CfbdGame,
    CfbdPlay,
    CfbdSyncRun,
    CfbdSyncStatus,
    CfbdTeamGameStat,
    CfbdTeamRow,
    CfbdWinProbability,
    Player,
    User,
)
from app.workload import require_workload_capacity

router = APIRouter(prefix="/api/cfbd", tags=["cfbd"])

# The school name CFBD uses for the Toledo Rockets and the MAC conference label.
TOLEDO_SCHOOL = "Toledo"
MAC_CONFERENCE = "Mid-American"
SOURCE_LABEL = "CollegeFootballData.com"
SYNC_ENDPOINT_ALIASES = {
    "teams": ("teams", "/teams"),
    "games": ("games", "/games"),
    "drives": ("drives", "/drives"),
    "plays": ("plays", "/plays"),
    "team_game_stats": ("team_game_stats", "/games/teams"),
    "win_probability": ("win_probability", "/metrics/wp"),
}

# Read-policy dependency shared by every endpoint (coaching staff only).
_require_cfbd_read = require_policy(Resource.CFBD_ANALYTICS, Action.READ)


# The vendor calls below are explicit administrator actions. Ordinary reads
# continue to use the cache, including when the vendor is unavailable.
class CfbdSyncRequest(BaseModel):
    season: int = Field(ge=2000, le=2100)
    scope: Literal["teams", "schedule", "benchmark", "plays", "drives", "roster"]
    season_type: Literal["regular", "postseason"] = "regular"
    week: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def require_play_week(self) -> CfbdSyncRequest:
        if self.scope == "plays" and self.week is None:
            raise ValueError("Choose a week when refreshing play-by-play data.")
        return self


class CfbdConnectionStatus(BaseModel):
    configured: bool
    source: str = SOURCE_LABEL


class CfbdSyncResponse(BaseModel):
    scope: str
    season: int
    status: str
    rows_written: int
    message: str


async def _sync_lock() -> AsyncGenerator[None, None]:
    """Serialize vendor refreshes even across API replicas.

    Ingestion commits each cache table independently. Keep this transaction
    on its own connection so those commits cannot release the refresh lock.
    Cancellation/connection loss releases it without leaving a pooled lock.
    """
    from app.database import engine

    async with engine.begin() as connection:
        acquired = (
            await connection.execute(text("SELECT pg_try_advisory_xact_lock(722419608)"))
        ).scalar_one()
        if not acquired:
            raise HTTPException(
                409,
                "Another CFBD refresh is running. Wait for it to finish, then try again.",
                headers={"Retry-After": "15"},
            )
        yield


@router.get("/status", response_model=CfbdConnectionStatus)
async def cfbd_connection_status(
    _user: Annotated[User, Depends(_require_cfbd_read)],
) -> CfbdConnectionStatus:
    """Report readiness without ever returning the credential or vendor URL."""
    return CfbdConnectionStatus(configured=bool(get_settings().cfbd_api_key.strip()))


@router.post("/sync", response_model=CfbdSyncResponse)
async def refresh_cfbd_cache(
    body: CfbdSyncRequest,
    _user: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    _lock: Annotated[None, Depends(_sync_lock)],
) -> CfbdSyncResponse:
    """Refresh one bounded scope, preserving cached data on vendor failure."""
    from app.cfbd.client import CFBDClient
    from app.cfbd.roster_sync import current_season, sync_player_seasons, sync_roster_and_stats
    from app.cfbd.sync import (
        sync_drives,
        sync_games,
        sync_plays,
        sync_team_game_stats,
        sync_teams,
    )

    if not get_settings().cfbd_api_key.strip():
        raise HTTPException(503, "CFBD is not connected. Configure its key on the team server.")
    # Interactive refreshes are deliberately smaller than a whole-season CLI
    # sync, and have a bounded retry/timeout budget per upstream call.
    async with CFBDClient.from_settings(timeout=10.0, max_retries=1) as client:
        options: dict[str, Any] = {"season": body.season, "season_type": body.season_type}
        if body.scope == "teams":
            result = await sync_teams(db, client, **options)
        elif body.scope == "schedule":
            result = await sync_games(db, client, **options)
        elif body.scope == "benchmark":
            result = await sync_team_game_stats(db, client, **options)
        elif body.scope == "plays":
            weeks = [body.week] if body.week is not None else []
            result = await sync_plays(db, client, weeks=weeks, **options)
        elif body.scope == "roster":
            if body.season == current_season(datetime.now(UTC)):
                result = await sync_roster_and_stats(db, client, season=body.season)
            else:
                # The active roster always follows the current season; other
                # seasons only refresh the per-player season-stat cache.
                result = await sync_player_seasons(db, client, season=body.season)
        else:
            result = await sync_drives(db, client, **options)
    succeeded = result.status.value == "succeeded"
    return CfbdSyncResponse(
        scope=body.scope,
        season=body.season,
        status=result.status.value,
        rows_written=result.rows_written,
        message=(
            f"Refreshed {result.rows_written} cached records."
            if succeeded
            else (
                "CFBD could not refresh all requested data. "
                "Previously cached records remain available."
            )
        ),
    )


# ── Schemas ───────────────────────────────────────────────────────────────────


class CfbdCacheMeta(BaseModel):
    """Freshness metadata so the UI can render empty/stale/unavailable states."""

    source: str = SOURCE_LABEL
    source_endpoint: str
    # "ok" | "partial" | "error" | "running" | "never"
    sync_status: str
    last_synced_at: datetime | None
    stale: bool
    stale_after_hours: int
    row_count: int


class CfbdTeamOut(BaseModel):
    cfbd_team_id: int | None
    school: str
    mascot: str | None
    abbreviation: str | None
    conference: str | None
    division: str | None
    color: str | None
    alt_color: str | None


class CfbdTeamResponse(BaseModel):
    team: CfbdTeamOut | None
    cache: CfbdCacheMeta


class CfbdGameOut(BaseModel):
    cfbd_game_id: int
    season: int
    week: int | None
    season_type: str | None
    start_date: datetime | None
    home_team: str | None
    away_team: str | None
    home_points: int | None
    away_points: int | None
    venue: str | None
    completed: bool | None


class CfbdScheduleResponse(BaseModel):
    season: int | None
    games: list[CfbdGameOut]
    cache: CfbdCacheMeta


class CfbdPlayOut(BaseModel):
    cfbd_play_id: int
    cfbd_drive_id: int | None
    offense: str | None
    defense: str | None
    period: int | None
    down: int | None
    distance: int | None
    yard_line: int | None
    yards_gained: int | None
    play_type: str | None
    play_text: str | None
    ppa: float | None


class CfbdPlaysResponse(BaseModel):
    game_id: int
    plays: list[CfbdPlayOut]
    cache: CfbdCacheMeta


class CfbdDriveOut(BaseModel):
    cfbd_drive_id: int
    offense: str | None
    defense: str | None
    drive_number: int | None
    scoring: bool | None
    start_period: int | None
    end_period: int | None
    plays: int | None
    yards: int | None
    drive_result: str | None


class CfbdDrivesResponse(BaseModel):
    game_id: int
    drives: list[CfbdDriveOut]
    cache: CfbdCacheMeta


class CfbdWinProbOut(BaseModel):
    play_number: int | None
    home_team: str | None
    away_team: str | None
    home_win_prob: float | None
    play_text: str | None


class CfbdWinProbResponse(BaseModel):
    game_id: int
    timeline: list[CfbdWinProbOut]
    cache: CfbdCacheMeta


class CfbdMacBenchmarkRow(BaseModel):
    team: str
    conference: str | None
    games: int
    avg_points_for: float | None
    avg_points_against: float | None
    point_differential: float | None


class CfbdMacBenchmarkResponse(BaseModel):
    season: int | None
    conference: str
    teams: list[CfbdMacBenchmarkRow]
    cache: CfbdCacheMeta


# ── Cache-metadata helper ──────────────────────────────────────────────────────


async def _cache_meta(db: AsyncSession, source_endpoint: str, *, row_count: int) -> CfbdCacheMeta:
    """Build the freshness block for ``source_endpoint`` from ``cfbd_sync_runs``."""
    settings = get_settings()
    stale_after = settings.cfbd_cache_stale_after_hours
    endpoint_names = SYNC_ENDPOINT_ALIASES.get(source_endpoint, (source_endpoint,))

    latest = (
        await db.execute(
            select(CfbdSyncRun)
            .where(CfbdSyncRun.endpoint.in_(endpoint_names))
            .order_by(desc(CfbdSyncRun.started_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    last_success = (
        await db.execute(
            select(CfbdSyncRun)
            .where(CfbdSyncRun.endpoint.in_(endpoint_names))
            .where(CfbdSyncRun.status.in_([CfbdSyncStatus.succeeded, CfbdSyncStatus.partial]))
            .order_by(desc(CfbdSyncRun.finished_at))
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest is None:
        sync_status = "never"
    else:
        sync_status = _public_sync_status(latest.status)

    last_synced_at = last_success.finished_at if last_success is not None else None

    if last_synced_at is None:
        stale = True
    else:
        reference = last_synced_at
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        stale = (datetime.now(UTC) - reference) > timedelta(hours=stale_after)

    return CfbdCacheMeta(
        source_endpoint=source_endpoint,
        sync_status=sync_status,
        last_synced_at=last_synced_at,
        stale=stale,
        stale_after_hours=stale_after,
        row_count=row_count,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/teams/toledo", response_model=CfbdTeamResponse)
async def get_toledo_team(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(_require_cfbd_read)],
) -> CfbdTeamResponse:
    """Return cached Toledo team metadata."""
    row = (
        await db.execute(select(CfbdTeamRow).where(CfbdTeamRow.school == TOLEDO_SCHOOL).limit(1))
    ).scalar_one_or_none()

    team = (
        CfbdTeamOut(
            cfbd_team_id=row.cfbd_team_id,
            school=row.school,
            mascot=row.mascot,
            abbreviation=row.abbreviation,
            conference=row.conference,
            division=row.division,
            color=row.color,
            alt_color=row.alt_color,
        )
        if row is not None
        else None
    )
    cache = await _cache_meta(db, "teams", row_count=1 if row is not None else 0)
    return CfbdTeamResponse(team=team, cache=cache)


@router.get("/teams/toledo/schedule", response_model=CfbdScheduleResponse)
async def get_toledo_schedule(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(_require_cfbd_read)],
    season: Annotated[int | None, Query(ge=1900, le=2100)] = None,
) -> CfbdScheduleResponse:
    """Return cached Toledo games (optionally filtered by ``season``)."""
    stmt = select(CfbdGame).where(
        (CfbdGame.home_team == TOLEDO_SCHOOL) | (CfbdGame.away_team == TOLEDO_SCHOOL)
    )
    if season is not None:
        stmt = stmt.where(CfbdGame.season == season)
    stmt = stmt.order_by(CfbdGame.start_date.asc().nullslast(), CfbdGame.week.asc().nullslast())

    rows = list((await db.execute(stmt)).scalars().all())
    games = [
        CfbdGameOut(
            cfbd_game_id=g.cfbd_game_id,
            season=g.season,
            week=g.week,
            season_type=g.season_type,
            start_date=g.start_date,
            home_team=g.home_team,
            away_team=g.away_team,
            home_points=g.home_points,
            away_points=g.away_points,
            venue=g.venue,
            completed=g.completed,
        )
        for g in rows
    ]
    cache = await _cache_meta(db, "games", row_count=len(games))
    return CfbdScheduleResponse(season=season, games=games, cache=cache)


@router.get("/games/{game_id}/plays", response_model=CfbdPlaysResponse)
async def get_game_plays(
    game_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(_require_cfbd_read)],
    limit: Annotated[int, Query(ge=1, le=500)] = 300,
) -> CfbdPlaysResponse:
    """Return cached play-by-play for a single game."""
    rows = list(
        (
            await db.execute(
                select(CfbdPlay)
                .where(CfbdPlay.cfbd_game_id == game_id)
                .order_by(CfbdPlay.cfbd_play_id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    plays = [
        CfbdPlayOut(
            cfbd_play_id=p.cfbd_play_id,
            cfbd_drive_id=p.cfbd_drive_id,
            offense=p.offense,
            defense=p.defense,
            period=p.period,
            down=p.down,
            distance=p.distance,
            yard_line=p.yard_line,
            yards_gained=p.yards_gained,
            play_type=p.play_type,
            play_text=p.play_text,
            ppa=p.ppa,
        )
        for p in rows
    ]
    cache = await _cache_meta(db, "plays", row_count=len(plays))
    return CfbdPlaysResponse(game_id=game_id, plays=plays, cache=cache)


@router.get("/games/{game_id}/drives", response_model=CfbdDrivesResponse)
async def get_game_drives(
    game_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(_require_cfbd_read)],
) -> CfbdDrivesResponse:
    """Return cached drive summaries for a single game."""
    rows = list(
        (
            await db.execute(
                select(CfbdDrive)
                .where(CfbdDrive.cfbd_game_id == game_id)
                .order_by(CfbdDrive.drive_number.asc().nullslast(), CfbdDrive.cfbd_drive_id.asc())
            )
        )
        .scalars()
        .all()
    )
    drives = [
        CfbdDriveOut(
            cfbd_drive_id=d.cfbd_drive_id,
            offense=d.offense,
            defense=d.defense,
            drive_number=d.drive_number,
            scoring=d.scoring,
            start_period=d.start_period,
            end_period=d.end_period,
            plays=d.plays,
            yards=d.yards,
            drive_result=d.drive_result,
        )
        for d in rows
    ]
    cache = await _cache_meta(db, "drives", row_count=len(drives))
    return CfbdDrivesResponse(game_id=game_id, drives=drives, cache=cache)


@router.get("/games/{game_id}/win-probability", response_model=CfbdWinProbResponse)
async def get_game_win_probability(
    game_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(_require_cfbd_read)],
) -> CfbdWinProbResponse:
    """Return the cached win-probability timeline for a single game."""
    rows = list(
        (
            await db.execute(
                select(CfbdWinProbability)
                .where(CfbdWinProbability.cfbd_game_id == game_id)
                .order_by(CfbdWinProbability.play_number.asc().nullslast())
            )
        )
        .scalars()
        .all()
    )
    timeline = [
        CfbdWinProbOut(
            play_number=w.play_number,
            home_team=w.home_team,
            away_team=w.away_team,
            home_win_prob=w.home_win_prob,
            play_text=w.play_text,
        )
        for w in rows
    ]
    cache = await _cache_meta(db, "win_probability", row_count=len(timeline))
    return CfbdWinProbResponse(game_id=game_id, timeline=timeline, cache=cache)


@router.get("/mac/benchmark", response_model=CfbdMacBenchmarkResponse)
async def get_mac_benchmark(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(_require_cfbd_read)],
    # Heavy: aggregates box scores across the whole conference. Returns a
    # distinguishable 503 workload_gated under load.
    _capacity: Annotated[Any, Depends(require_workload_capacity("cfbd.mac_benchmark"))],
    season: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    conference: Annotated[str, Query(min_length=1, max_length=64)] = MAC_CONFERENCE,
) -> CfbdMacBenchmarkResponse:
    """Aggregate cached team game stats into a per-team conference benchmark."""
    stmt = select(CfbdTeamGameStat).where(CfbdTeamGameStat.conference == conference)
    if season is not None:
        stmt = stmt.where(CfbdTeamGameStat.season == season)
    rows = list((await db.execute(stmt)).scalars().all())
    points_by_game_team: dict[tuple[int, str], int] = {}
    for r in rows:
        if getattr(r, "cfbd_game_id", None) is None or r.points is None:
            continue
        points_by_game_team[(r.cfbd_game_id, r.team)] = r.points

    acc: dict[str, dict[str, float]] = {}
    for r in rows:
        bucket = acc.setdefault(
            r.team, {"games": 0.0, "pf": 0.0, "pa": 0.0, "pf_n": 0.0, "pa_n": 0.0}
        )
        bucket["games"] += 1
        if r.points is not None:
            bucket["pf"] += r.points
            bucket["pf_n"] += 1
        opp_points = _opponent_points(r, points_by_game_team)
        if opp_points is not None:
            bucket["pa"] += opp_points
            bucket["pa_n"] += 1

    teams: list[CfbdMacBenchmarkRow] = []
    for team, b in acc.items():
        avg_for = b["pf"] / b["pf_n"] if b["pf_n"] else None
        avg_against = b["pa"] / b["pa_n"] if b["pa_n"] else None
        diff = avg_for - avg_against if avg_for is not None and avg_against is not None else None
        teams.append(
            CfbdMacBenchmarkRow(
                team=team,
                conference=conference,
                games=int(b["games"]),
                avg_points_for=_round(avg_for),
                avg_points_against=_round(avg_against),
                point_differential=_round(diff),
            )
        )

    teams.sort(key=lambda t: (t.point_differential is None, -(t.point_differential or 0.0), t.team))
    cache = await _cache_meta(db, "team_game_stats", row_count=len(rows))
    return CfbdMacBenchmarkResponse(season=season, conference=conference, teams=teams, cache=cache)


def _public_sync_status(status: Any) -> str:
    value = getattr(status, "value", status)
    if value == "succeeded":
        return "ok"
    if value == "failed":
        return "error"
    return str(value)


def _opponent_points(
    row: CfbdTeamGameStat, points_by_game_team: dict[tuple[int, str], int]
) -> int | None:
    """Look up the opponent's score for ``row`` from the same game."""
    opponent = getattr(row, "opponent", None)
    if row.cfbd_game_id is not None and opponent:
        return points_by_game_team.get((row.cfbd_game_id, opponent))
    return None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


# ── Player season data (profile "College stats" tab) ─────────────────────────


class CfbdPlayerSeasonOut(BaseModel):
    season: int
    team: str | None
    position: str | None
    stats: dict[str, dict[str, Any]] | None
    usage: dict[str, Any] | None
    ppa: dict[str, Any] | None


class CfbdPlayerSeasonsResponse(BaseModel):
    player_id: str
    cfbd_athlete_id: str | None
    seasons: list[CfbdPlayerSeasonOut]


@router.get("/players/{player_id}/seasons", response_model=CfbdPlayerSeasonsResponse)
async def get_player_seasons(
    player_id: uuid.UUID,
    _user: Annotated[User, Depends(_require_cfbd_read)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CfbdPlayerSeasonsResponse:
    """A rostered player's CFBD seasons, newest first. Empty when the player
    isn't linked to CFBD (e.g. a walk-on added by a coach)."""
    player = (await db.execute(select(Player).where(Player.id == player_id))).scalar_one_or_none()
    if player is None:
        raise HTTPException(404, "Player not found")
    athlete = (player.metadata_ or {}).get("cfbd_athlete_id")
    rows: list[CFBDPlayerSeason] = []
    if athlete:
        rows = list(
            (
                await db.execute(
                    select(CFBDPlayerSeason)
                    .where(CFBDPlayerSeason.athlete_id == int(athlete))
                    .order_by(desc(CFBDPlayerSeason.season))
                )
            ).scalars()
        )
    return CfbdPlayerSeasonsResponse(
        player_id=str(player.id),
        cfbd_athlete_id=str(athlete) if athlete else None,
        seasons=[
            CfbdPlayerSeasonOut(
                season=row.season,
                team=row.team,
                position=row.position,
                stats=row.stats,
                usage=row.usage,
                ppa=row.ppa,
            )
            for row in rows
        ],
    )


# ── Game film <-> CFBD play-by-play ──────────────────────────────────────────


@router.post("/videos/{video_id}/link")
async def link_game_film(
    video_id: uuid.UUID,
    _user: Annotated[User, Depends(require_coach_or_above)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Match a processed game film's plays to CFBD play-by-play.

    Each matched clip gets the official play type, yards gained, result text
    and PPA under ``label_data.cfbd``. Downloads the game's play-by-play first
    when it isn't cached. Safe to repeat.
    """
    from app.cfbd.game_link import ensure_plays, find_game, link_video
    from app.models import Video

    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Film not found")
    game = await find_game(db, video)
    if game is None:
        raise HTTPException(
            404,
            "No Toledo game in CollegeFootballData on this film's date. "
            "Check the film's date, or refresh the season schedule.",
        )
    if not await ensure_plays(db, game):
        raise HTTPException(503, "CFBD play-by-play for this game isn't available yet.")
    result = await link_video(db, video_id)
    await db.commit()
    return result
