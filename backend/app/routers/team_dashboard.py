"""Team performance dashboard: the Home page's one call.

Rolls the per-rep ``physical_profile`` metrics up by week (practice vs game)
and by position group, adds every linked game's breakdown, the workload /
injury-risk view the caller's role may see, and this week's film activity.

Team totals count every Toledo track, not only identified players (player
identification is still sparse): in practice both units are Toledo, in a
game only the side Toledo is on for that play (``clips.our_possession``).
Position groups use the identified player's group, else the track's own
role label from the labels stage.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Float, String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff
from app.governance import Action, Resource, policy_allows
from app.models import (
    Clip,
    FootballSession,
    Metric,
    Player,
    PlayerWorkloadDaily,
    SessionKind,
    Tracklet,
    User,
    Video,
    VideoStatus,
)
from app.routers.game_summary import build_game_summary
from app.routers.health_workload import PLAYER_LEVEL_RISK_ROLES, _position_group_aggregates

router = APIRouter(prefix="/api/v1/team", tags=["team-dashboard"])

PHYSICAL = "physical_profile"
_RECORDED = func.coalesce(Video.recorded_at, Video.created_at)
#: Workload risk at or above this is flagged for sports-performance staff.
RISK_FLAG = 0.6


class WeekPoint(BaseModel):
    week_start: dt.date
    session_kind: str
    reps: int
    players: int
    yards_per_rep: float | None
    high_speed_yards_per_rep: float | None
    sprints_per_rep: float | None
    top_speed_p90_yps: float | None
    get_off_yards: float | None


class GroupWeek(BaseModel):
    week_start: dt.date
    reps: int
    top_speed_p90_yps: float | None
    high_speed_yards_per_rep: float | None
    get_off_yards: float | None


class PositionGroupTrend(BaseModel):
    position_group: str
    weekly: list[GroupWeek]
    #: This week's value minus the previous measured week's (None if either is missing).
    change: dict[str, float | None]


class GameSide(BaseModel):
    plays: int
    yards_per_play: float | None
    success_rate: float | None
    epa_per_play: float | None
    third_down_rate: float | None
    explosive_plays: int
    turnovers: int


class GameRow(BaseModel):
    video_id: str
    date: dt.date | None
    opponent: str | None
    linked_plays: int
    offense: GameSide | None
    defense: GameSide | None


class Readiness(BaseModel):
    visible: bool
    player_level: bool = False
    as_of: dt.date | None = None
    groups: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []


class Activity(BaseModel):
    sessions_this_week: int
    plays_this_week: int
    film_processing: int
    film_uploaded: int


class TeamDashboard(BaseModel):
    weekly: list[WeekPoint]
    position_groups: list[PositionGroupTrend]
    games: list[GameRow]
    readiness: Readiness
    activity: Activity


def _num(key: str) -> Any:
    return Metric.metric_value[key].astext.cast(Float)


def _toledo_track() -> Any:
    """Tracks that are Toledo players (see module docstring)."""
    practice_sides = Tracklet.team_label.in_(("offense", "defense", "home"))
    game_side = or_(
        Tracklet.team_label == "home",
        Tracklet.team_label == cast(Clip.our_possession, String),
    )
    return or_(
        Tracklet.player_id.is_not(None),
        and_(Clip.session_kind != SessionKind.game, practice_sides),
        and_(Clip.session_kind == SessionKind.game, game_side),
    )


def _physical_base(since: dt.datetime, session_kind: SessionKind | None) -> Any:
    stmt = (
        select()
        .select_from(Metric)
        .join(Tracklet, Tracklet.id == Metric.tracklet_id)
        .join(Clip, Clip.id == Tracklet.clip_id)
        .join(Video, Video.id == Clip.video_id)
        .where(
            Metric.metric_name == PHYSICAL,
            Metric.is_suppressed.is_(False),
            Metric.experimental_flag.is_(False),
            _RECORDED >= since,
            _toledo_track(),
        )
    )
    if session_kind is not None:
        stmt = stmt.where(Clip.session_kind == session_kind)
    return stmt


def _per(total: Any, count: int) -> float | None:
    return round(float(total) / count, 2) if total is not None and count else None


def _round(value: Any, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None else None


async def _weekly(
    db: AsyncSession, since: dt.datetime, session_kind: SessionKind | None
) -> list[WeekPoint]:
    week = func.date_trunc("week", _RECORDED)
    kind = func.coalesce(cast(Clip.session_kind, String), "practice")
    stmt = (
        _physical_base(since, session_kind)
        .add_columns(
            week.label("week"),
            kind.label("kind"),
            func.count(Metric.id).label("reps"),
            func.count(func.distinct(Tracklet.player_id)).label("players"),
            func.sum(_num("yards")).label("yards"),
            func.sum(_num("high_speed_yards")).label("hsy"),
            func.sum(_num("sprints")).label("sprints"),
            func.percentile_cont(0.9).within_group(_num("top_speed_yps")).label("top_p90"),
            func.avg(_num("get_off_yards")).label("get_off"),
        )
        .group_by(week, kind)
        .order_by(week, kind)
    )
    rows = (await db.execute(stmt)).all()
    return [
        WeekPoint(
            week_start=row.week.date(),
            session_kind=str(row.kind),
            reps=int(row.reps),
            players=int(row.players),
            yards_per_rep=_per(row.yards, row.reps),
            high_speed_yards_per_rep=_per(row.hsy, row.reps),
            sprints_per_rep=_per(row.sprints, row.reps),
            top_speed_p90_yps=_round(row.top_p90),
            get_off_yards=_round(row.get_off),
        )
        for row in rows
    ]


async def _position_groups(
    db: AsyncSession, since: dt.datetime, session_kind: SessionKind | None
) -> list[PositionGroupTrend]:
    week = func.date_trunc("week", _RECORDED)
    group = func.upper(func.coalesce(Player.position_group, Tracklet.position_group))
    stmt = (
        _physical_base(since, session_kind)
        .outerjoin(Player, Player.id == Tracklet.player_id)
        .add_columns(
            group.label("group"),
            week.label("week"),
            func.count(Metric.id).label("reps"),
            func.percentile_cont(0.9).within_group(_num("top_speed_yps")).label("top_p90"),
            func.sum(_num("high_speed_yards")).label("hsy"),
            func.avg(_num("get_off_yards")).label("get_off"),
        )
        .where(group.is_not(None))
        .group_by(group, week)
        .order_by(group, week)
    )
    by_group: dict[str, list[GroupWeek]] = {}
    for row in (await db.execute(stmt)).all():
        by_group.setdefault(str(row.group), []).append(
            GroupWeek(
                week_start=row.week.date(),
                reps=int(row.reps),
                top_speed_p90_yps=_round(row.top_p90),
                high_speed_yards_per_rep=_per(row.hsy, row.reps),
                get_off_yards=_round(row.get_off),
            )
        )
    trends = []
    for name, weeks in sorted(by_group.items()):
        latest, previous = (weeks[-1], weeks[-2]) if len(weeks) >= 2 else (None, None)
        change = {
            key: (
                round(getattr(latest, key) - getattr(previous, key), 2)
                if latest
                and previous
                and getattr(latest, key) is not None
                and getattr(previous, key) is not None
                else None
            )
            for key in ("top_speed_p90_yps", "high_speed_yards_per_rep", "get_off_yards")
        }
        trends.append(PositionGroupTrend(position_group=name, weekly=weeks, change=change))
    return trends


def _side(summary: Any) -> GameSide | None:
    if summary is None:
        return None
    attempts = summary.third_down_attempts
    return GameSide(
        plays=summary.plays,
        yards_per_play=summary.yards_per_play,
        success_rate=summary.success_rate,
        epa_per_play=summary.epa_per_play,
        third_down_rate=(round(summary.third_down_conversions / attempts, 3) if attempts else None),
        explosive_plays=summary.explosive_plays,
        turnovers=summary.turnovers,
    )


async def _games(db: AsyncSession, since: dt.datetime) -> list[GameRow]:
    videos = list(
        (
            await db.execute(
                select(Video)
                .where(Video.session_kind == SessionKind.game, _RECORDED >= since)
                .order_by(_RECORDED)
            )
        ).scalars()
    )
    rows = []
    for video in videos:
        summary = await build_game_summary(db, video)
        if summary.linked_plays == 0:
            continue
        played = video.recorded_at or video.created_at
        rows.append(
            GameRow(
                video_id=str(video.id),
                date=played.date() if played else None,
                opponent=summary.opponent,
                linked_plays=summary.linked_plays,
                offense=_side(summary.offense),
                defense=_side(summary.defense),
            )
        )
    return rows


async def _readiness(db: AsyncSession, user: User) -> Readiness:
    if not policy_allows(user.role, Resource.HEALTH_WORKLOAD, Action.READ):
        return Readiness(visible=False)
    as_of = (await db.execute(select(func.max(PlayerWorkloadDaily.date)))).scalar_one_or_none()
    if as_of is None:
        return Readiness(visible=True, player_level=user.role in PLAYER_LEVEL_RISK_ROLES)
    pairs = list(
        (
            await db.execute(
                select(PlayerWorkloadDaily, Player)
                .join(Player, PlayerWorkloadDaily.player_id == Player.id)
                .where(PlayerWorkloadDaily.date > as_of - dt.timedelta(days=7))
                .where(PlayerWorkloadDaily.date <= as_of)
            )
        ).all()
    )
    player_level = user.role in PLAYER_LEVEL_RISK_ROLES
    flagged: list[dict[str, Any]] = []
    if player_level:
        latest: dict[Any, tuple[Any, Any]] = {}
        for row, player in pairs:
            if row.player_id not in latest or row.date > latest[row.player_id][0].date:
                latest[row.player_id] = (row, player)
        for row, player in latest.values():
            risk = row.injury_risk_score
            if risk is not None and risk >= RISK_FLAG:
                flagged.append(
                    {
                        "player_id": str(player.id),
                        "name": f"{player.first_name} {player.last_name}",
                        "jersey_number": player.jersey_number,
                        "position_group": player.position_group,
                        "injury_risk_score": round(float(risk), 3),
                        "acwr": _round(row.acwr, 3),
                    }
                )
        flagged.sort(key=lambda item: -item["injury_risk_score"])
    return Readiness(
        visible=True,
        player_level=player_level,
        as_of=as_of,
        groups=_position_group_aggregates(pairs),
        flagged=flagged[:8],
    )


async def _activity(db: AsyncSession) -> Activity:
    week_ago = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    session_date = func.coalesce(FootballSession.recorded_at, FootballSession.created_at)
    sessions = (
        await db.execute(select(func.count(FootballSession.id)).where(session_date >= week_ago))
    ).scalar_one()
    plays = (
        await db.execute(
            select(func.count(func.distinct(func.concat(Clip.video_id, ":", Clip.play_number))))
            .join(Video, Video.id == Clip.video_id)
            .where(_RECORDED >= week_ago)
        )
    ).scalar_one()
    processing = (
        await db.execute(select(func.count(Video.id)).where(Video.status == VideoStatus.processing))
    ).scalar_one()
    uploaded = (
        await db.execute(select(func.count(Video.id)).where(Video.status == VideoStatus.uploaded))
    ).scalar_one()
    return Activity(
        sessions_this_week=int(sessions),
        plays_this_week=int(plays),
        film_processing=int(processing),
        film_uploaded=int(uploaded),
    )


@router.get("/dashboard", response_model=TeamDashboard)
async def team_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_any_staff)],
    weeks: int = Query(default=12, ge=1, le=52, description="Trailing weeks of film"),
    session_kind: SessionKind | None = Query(default=None),
) -> TeamDashboard:
    """Everything the Home dashboard shows, in one call."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(weeks=weeks)
    return TeamDashboard(
        weekly=await _weekly(db, since, session_kind),
        position_groups=await _position_groups(db, since, session_kind),
        games=await _games(db, since),
        readiness=await _readiness(db, user),
        activity=await _activity(db),
    )
