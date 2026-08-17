"""Per-player tracking analytics — the roster's "real numbers" read surface.

Aggregates attributed tracklets and their coach-visible metrics per player so
the roster and profile views can show tracked-film counts, identity
confidence, and speed/distance aggregates in one batched call (one HTTP
round trip and two grouped queries for the whole roster — never N+1).

Confidence language (calibrated, never fabricated):
    * ``tracking_confidence`` is exactly what its name says — the span-weighted
      mean of ``tracklets.track_confidence`` (detector/tracker quality). It is
      deliberately NOT called identity confidence: identity is a separate
      signal (Issue-flagged in review) and jersey OCR is never ground truth.
    * ``identity_bucket``: ``known`` only when a human confirmed this player's
      identity (a ``player_identity`` coach correction naming the player);
      ``probable`` when unconfirmed but tracking confidence clears the profile
      threshold; ``needs_review`` otherwise.

Metric visibility matches :mod:`app.routers.overlays`: suppressed metrics are
never aggregated, and experimental metrics are excluded from these aggregate
columns entirely (an aggregate strips the per-metric review context that
experimental values require).

Endpoints:
    GET /api/v1/players/metrics/summary        — batched, whole roster
    GET /api/v1/players/{id}/metrics/summary   — one player + weekly trend
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Float, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.governance import Action, Resource, require_policy
from app.models import (
    Clip,
    CoachCorrection,
    CorrectionType,
    Metric,
    Player,
    PlayerIdentityState,
    SessionKind,
    Tracklet,
    User,
    UserRole,
    Video,
)

router = APIRouter(tags=["player-metrics"])

# Metric names aggregated into roster columns. Both are per-tracklet pipeline
# metrics (stage_metrics) whose values live in metric_value JSON.
_MAX_SPEED = "max_speed"
_DISTANCE = "distance_traveled"


# ── Schemas ───────────────────────────────────────────────────────────────────


class PlayerMetricsSummary(BaseModel):
    """Aggregates for one player; fields are None when no data exists."""

    player_id: uuid.UUID
    tracklet_count: int
    tracked_clip_count: int
    last_tracked_at: datetime | None
    # Span-weighted mean of tracklets.track_confidence (frames-long tracklets
    # count for more than two-frame fragments). Tracking quality, not identity.
    tracking_confidence: float | None
    identity_bucket: PlayerIdentityState
    max_speed_yps: float | None
    max_speed_samples: int
    distance_yards: float | None
    distance_samples: int


class PlayerMetricsWeekly(BaseModel):
    """One week's aggregates for the profile trend line."""

    week_start: datetime
    tracked_clip_count: int
    tracking_confidence: float | None
    max_speed_yps: float | None
    distance_yards: float | None


class PlayerMetricsDetail(BaseModel):
    summary: PlayerMetricsSummary
    weekly: list[PlayerMetricsWeekly]


# ── Helpers ───────────────────────────────────────────────────────────────────


def identity_bucket_for(
    confidence: float | None, threshold: float, *, human_confirmed: bool = False
) -> PlayerIdentityState:
    """Map aggregated signals to the existing identity vocabulary.

    ``known`` requires a human confirmation (a ``player_identity`` coach
    correction naming this player) — model confidence alone never yields it,
    because jersey OCR is never trusted as ground truth.
    """
    if human_confirmed:
        return PlayerIdentityState.known
    if confidence is None or confidence < threshold:
        return PlayerIdentityState.needs_review
    return PlayerIdentityState.probable


_SPAN = Tracklet.end_frame - Tracklet.start_frame + 1
_RECORDED = func.coalesce(Video.recorded_at, Video.created_at)


def _apply_film_filters(
    stmt: Select[tuple[Any, ...]],
    session_kind: SessionKind | None,
    since: datetime | None,
) -> Select[tuple[Any, ...]]:
    if session_kind is not None:
        stmt = stmt.where(Clip.session_kind == session_kind)
    if since is not None:
        stmt = stmt.where(_RECORDED >= since)
    return stmt


def _tracklet_agg_stmt(
    session_kind: SessionKind | None, since: datetime | None
) -> Select[tuple[Any, ...]]:
    """Per-player tracklet aggregates (count, clips, span-weighted confidence)."""
    conf_weight = case((Tracklet.track_confidence.is_not(None), _SPAN), else_=0)
    stmt = (
        select(
            Tracklet.player_id.label("pid"),
            func.count(Tracklet.id).label("tracklet_count"),
            func.count(func.distinct(Tracklet.clip_id)).label("clip_count"),
            (
                func.sum(func.coalesce(Tracklet.track_confidence, 0.0) * conf_weight)
                / func.nullif(func.sum(conf_weight), 0)
            ).label("tracking_confidence"),
            func.max(_RECORDED).label("last_tracked_at"),
        )
        .join(Clip, Clip.id == Tracklet.clip_id)
        .join(Video, Video.id == Clip.video_id)
        .where(Tracklet.player_id.is_not(None))
        .group_by(Tracklet.player_id)
    )
    return _apply_film_filters(stmt, session_kind, since)


def _metric_agg_stmt(
    session_kind: SessionKind | None, since: datetime | None
) -> Select[tuple[Any, ...]]:
    """Per-player metric aggregates over coach-visible rows only."""
    speed_value = Metric.metric_value["yards_per_second"].astext.cast(Float)
    distance_value = Metric.metric_value["yards"].astext.cast(Float)
    stmt = (
        select(
            Tracklet.player_id.label("pid"),
            func.max(case((Metric.metric_name == _MAX_SPEED, speed_value))).label("max_speed"),
            func.count(case((Metric.metric_name == _MAX_SPEED, 1))).label("max_speed_samples"),
            func.sum(case((Metric.metric_name == _DISTANCE, distance_value))).label("distance"),
            func.count(case((Metric.metric_name == _DISTANCE, 1))).label("distance_samples"),
        )
        .select_from(Metric)
        .join(Tracklet, Tracklet.id == Metric.tracklet_id)
        .join(Clip, Clip.id == Tracklet.clip_id)
        .join(Video, Video.id == Clip.video_id)
        .where(
            Tracklet.player_id.is_not(None),
            Metric.is_suppressed.is_(False),
            Metric.experimental_flag.is_(False),
            Metric.metric_name.in_((_MAX_SPEED, _DISTANCE)),
        )
        .group_by(Tracklet.player_id)
    )
    return _apply_film_filters(stmt, session_kind, since)


def _build_summary(
    player_id: uuid.UUID,
    track_row: object | None,
    metric_row: object | None,
    threshold: float,
    *,
    human_confirmed: bool = False,
) -> PlayerMetricsSummary:
    confidence = getattr(track_row, "tracking_confidence", None)
    confidence_val = float(confidence) if confidence is not None else None
    max_speed = getattr(metric_row, "max_speed", None)
    distance = getattr(metric_row, "distance", None)
    return PlayerMetricsSummary(
        player_id=player_id,
        tracklet_count=getattr(track_row, "tracklet_count", 0) or 0,
        tracked_clip_count=getattr(track_row, "clip_count", 0) or 0,
        last_tracked_at=getattr(track_row, "last_tracked_at", None),
        tracking_confidence=confidence_val,
        identity_bucket=identity_bucket_for(
            confidence_val, threshold, human_confirmed=human_confirmed
        ),
        max_speed_yps=float(max_speed) if max_speed is not None else None,
        max_speed_samples=getattr(metric_row, "max_speed_samples", 0) or 0,
        distance_yards=float(distance) if distance is not None else None,
        distance_samples=getattr(metric_row, "distance_samples", 0) or 0,
    )


async def _player_scope_id(db: AsyncSession, user: User) -> uuid.UUID | None:
    """Player-role callers only ever see their own record.

    Returns the player id linked to a player-role account (None when the
    account has no linked player row — such a caller sees nothing). Staff
    roles return None *and* are exempted by the caller.
    """
    result = await db.execute(select(Player.id).where(Player.user_id == user.id))
    return result.scalar_one_or_none()


async def _human_confirmed_player_ids(db: AsyncSession) -> set[uuid.UUID]:
    """Player ids a coach explicitly confirmed via a player_identity correction."""
    stmt = select(
        CoachCorrection.corrected_value["player_id"].astext,
    ).where(
        CoachCorrection.correction_type == CorrectionType.player_identity,
        CoachCorrection.corrected_value["player_id"].astext.is_not(None),
    )
    out: set[uuid.UUID] = set()
    for (raw,) in (await db.execute(stmt)).all():
        try:
            out.add(uuid.UUID(raw))
        except (TypeError, ValueError):
            continue
    return out


_SessionKindQuery = Annotated[
    SessionKind | None,
    Query(description="Restrict aggregates to one session kind (practice/game/...)."),
]
_SinceQuery = Annotated[
    datetime | None,
    Query(description="Only count film recorded at or after this timestamp."),
]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/v1/players/metrics/summary")
async def list_player_metrics_summaries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAYER_METRICS, Action.READ))],
    session_kind: _SessionKindQuery = None,
    since: _SinceQuery = None,
) -> list[PlayerMetricsSummary]:
    """Batched aggregates for every player with at least one attributed tracklet.

    Players with no tracked film are simply absent — the UI renders its honest
    empty state for them rather than a fabricated zero. Player-role callers
    are scoped to their own record (the PLAYER_METRICS policy admits players,
    but only for themselves).
    """
    threshold = get_settings().player_profile_identity_confidence_threshold

    own_player_id: uuid.UUID | None = None
    if current_user.role == UserRole.player:
        own_player_id = await _player_scope_id(db, current_user)
        if own_player_id is None:
            return []

    track_stmt = _tracklet_agg_stmt(session_kind, since)
    metric_stmt = _metric_agg_stmt(session_kind, since)
    if own_player_id is not None:
        track_stmt = track_stmt.where(Tracklet.player_id == own_player_id)
        metric_stmt = metric_stmt.where(Tracklet.player_id == own_player_id)

    track_rows = (await db.execute(track_stmt)).all()
    metric_rows = (await db.execute(metric_stmt)).all()
    confirmed = await _human_confirmed_player_ids(db)

    metrics_by_pid = {row.pid: row for row in metric_rows}
    summaries = [
        _build_summary(
            row.pid,
            row,
            metrics_by_pid.get(row.pid),
            threshold,
            human_confirmed=row.pid in confirmed,
        )
        for row in track_rows
    ]
    summaries.sort(key=lambda s: str(s.player_id))
    return summaries


@router.get("/api/v1/players/{player_id}/metrics/summary")
async def get_player_metrics_summary(
    player_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAYER_METRICS, Action.READ))],
    session_kind: _SessionKindQuery = None,
    since: _SinceQuery = None,
) -> PlayerMetricsDetail:
    """One player's aggregates plus a weekly series for the trend line.

    Always returns a summary object (zero counts, ``needs_review`` bucket)
    even for players with no tracked film, so the profile page can render its
    empty state from the same shape. Player-role callers may only request
    their own record — anything else 404s so existence never leaks.
    """
    if current_user.role == UserRole.player:
        own_player_id = await _player_scope_id(db, current_user)
        if own_player_id is None or own_player_id != player_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    threshold = get_settings().player_profile_identity_confidence_threshold

    track_stmt = _tracklet_agg_stmt(session_kind, since).where(Tracklet.player_id == player_id)
    metric_stmt = _metric_agg_stmt(session_kind, since).where(Tracklet.player_id == player_id)
    track_row = (await db.execute(track_stmt)).one_or_none()
    metric_row = (await db.execute(metric_stmt)).one_or_none()

    week = func.date_trunc("week", _RECORDED)
    conf_weight = case((Tracklet.track_confidence.is_not(None), _SPAN), else_=0)
    weekly_stmt = (
        select(
            week.label("week_start"),
            func.count(func.distinct(Tracklet.clip_id)).label("clip_count"),
            (
                func.sum(func.coalesce(Tracklet.track_confidence, 0.0) * conf_weight)
                / func.nullif(func.sum(conf_weight), 0)
            ).label("tracking_confidence"),
        )
        .join(Clip, Clip.id == Tracklet.clip_id)
        .join(Video, Video.id == Clip.video_id)
        .where(Tracklet.player_id == player_id)
        .group_by(week)
        .order_by(week)
    )
    weekly_stmt = _apply_film_filters(weekly_stmt, session_kind, since)
    weekly_track = (await db.execute(weekly_stmt)).all()

    speed_value = Metric.metric_value["yards_per_second"].astext.cast(Float)
    distance_value = Metric.metric_value["yards"].astext.cast(Float)
    weekly_metric_stmt = (
        select(
            week.label("week_start"),
            func.max(case((Metric.metric_name == _MAX_SPEED, speed_value))).label("max_speed"),
            func.sum(case((Metric.metric_name == _DISTANCE, distance_value))).label("distance"),
        )
        .select_from(Metric)
        .join(Tracklet, Tracklet.id == Metric.tracklet_id)
        .join(Clip, Clip.id == Tracklet.clip_id)
        .join(Video, Video.id == Clip.video_id)
        .where(
            Tracklet.player_id == player_id,
            Metric.is_suppressed.is_(False),
            Metric.experimental_flag.is_(False),
            Metric.metric_name.in_((_MAX_SPEED, _DISTANCE)),
        )
        .group_by(week)
        .order_by(week)
    )
    weekly_metric_stmt = _apply_film_filters(weekly_metric_stmt, session_kind, since)
    weekly_metrics = {row.week_start: row for row in (await db.execute(weekly_metric_stmt)).all()}

    confirmed = await _human_confirmed_player_ids(db)

    weekly = [
        PlayerMetricsWeekly(
            week_start=row.week_start,
            tracked_clip_count=row.clip_count,
            tracking_confidence=(
                float(row.tracking_confidence) if row.tracking_confidence is not None else None
            ),
            max_speed_yps=(
                float(m.max_speed)
                if (m := weekly_metrics.get(row.week_start)) is not None and m.max_speed is not None
                else None
            ),
            distance_yards=(
                float(m2.distance)
                if (m2 := weekly_metrics.get(row.week_start)) is not None
                and m2.distance is not None
                else None
            ),
        )
        for row in weekly_track
    ]

    return PlayerMetricsDetail(
        summary=_build_summary(
            player_id,
            track_row,
            metric_row,
            threshold,
            human_confirmed=player_id in confirmed,
        ),
        weekly=weekly,
    )
