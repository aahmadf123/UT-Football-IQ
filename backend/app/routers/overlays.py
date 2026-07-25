"""Clip overlays router — aggregates tracklets, labels, events, and metrics
into a single payload the Clip Review UI can render on top of a real clip.

This is the read-only "wire the plumbing" surface for the clip-review overlay
layer (Issue #104). Write paths for the underlying tables live in their own
routers (``tracklets.py``, ``labels.py``, ``events.py``, ``metrics.py``); this
module deliberately does not duplicate them.

The payload is shaped for direct frontend consumption:
  - ``tracklets[]`` carry their ``track_points[]`` inline so the overlay
    canvas can interpolate per-frame positions without a second request.
  - ``events[]`` are ordered by frame for timeline markers.
  - ``labels[]`` describe formation/coverage/route metadata for the clip.
  - ``metrics[]`` are filtered down to coach-visible rows (suppressed and
    experimental-unapproved metrics are excluded from the overlay surface).
  - ``layers_available`` is a hint flag set per overlay layer so the UI can
    render an explicit "degraded" state when a layer has no data.
"""

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user
from app.models import Clip, Event, FieldCalibration, Label, Metric, Tracklet, User, UserRole

log = structlog.get_logger(__name__)
router = APIRouter(tags=["overlays"])

#: Coach-readable explanations for calibration reason codes. Spatial metrics
#: are suppressed *with a reason the UI can show*, never silently — the
#: difference between "the product is broken" and "this camera angle doesn't
#: expose field lines" (ADR 0005).
_REASON_TEXT: dict[str, str] = {
    "insufficient_yard_lines": (
        "Field yard lines aren't visible enough from this camera angle, so "
        "spatial metrics (routes, coverage, pressure) are unavailable for this film."
    ),
    "insufficient_lines": (
        "Not enough field markings are visible to map players onto the field, "
        "so spatial metrics are unavailable for this film."
    ),
    "insufficient_structured_lines": (
        "Field markings couldn't be structured into a reliable field map, so "
        "spatial metrics are unavailable for this film."
    ),
    "insufficient_intersections": (
        "Too few yard-line/hash intersections are visible to calibrate the "
        "field, so spatial metrics are unavailable for this film."
    ),
    "low_inlier_ratio": (
        "The field mapping was too unstable to trust, so spatial metrics are "
        "unavailable for this film."
    ),
    "no_calibration": (
        "The field couldn't be calibrated from this footage, so spatial "
        "metrics are unavailable. Player tracking and clips are unaffected."
    ),
    "no_frames": "The footage couldn't be read for field calibration.",
}

_DEFAULT_UNSAFE_REASON = (
    "Field calibration wasn't reliable for this footage, so spatial metrics "
    "are unavailable. Player tracking and clips are unaffected."
)


def _reason_text(reason_codes: list[str] | None) -> str:
    for code in reason_codes or []:
        if code in _REASON_TEXT:
            return _REASON_TEXT[code]
    return _DEFAULT_UNSAFE_REASON


def _optional_float(value: Any) -> float | None:
    return value if isinstance(value, float) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


# ── Schemas ─────────────────────────────────────────────────────────────


class OverlayTrackPoint(BaseModel):
    frame_number: int
    field_x: float | None
    field_y: float | None
    bbox: list[float] | None
    detection_confidence: float | None


class OverlayTracklet(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    player_id: uuid.UUID | None
    start_frame: int
    end_frame: int
    track_confidence: float | None
    team_label: str | None
    position_group: str | None
    side_of_ball: str | None
    track_points: list[OverlayTrackPoint] = []


class OverlayEvent(BaseModel):
    id: uuid.UUID
    event_type: str
    frame_number: int | None
    timestamp_seconds: float | None
    attributes: dict[str, Any] | None


class OverlayLabel(BaseModel):
    id: uuid.UUID
    tracklet_id: uuid.UUID | None
    label_type: str
    label_value: dict[str, Any]
    source: str


class OverlayMetric(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    tracklet_id: uuid.UUID | None
    metric_name: str
    metric_value: dict[str, Any]
    unit: str | None
    confidence: float | None
    effort_zscore: float | None
    loaf_flag: bool | None


class OverlayLayersAvailable(BaseModel):
    tracklets: bool
    events: bool
    labels: bool
    metrics: bool


class OverlayCalibration(BaseModel):
    """Field-calibration state for the clip's video, with a coach-readable
    reason whenever spatial metrics are suppressed (never silent)."""

    analytics_safe: bool
    reason: str | None
    reason_codes: list[str]
    confidence: float | None


class ClipOverlayResponse(BaseModel):
    clip_id: uuid.UUID
    capture_regime: str | None
    tracklets: list[OverlayTracklet]
    events: list[OverlayEvent]
    labels: list[OverlayLabel]
    metrics: list[OverlayMetric]
    layers_available: OverlayLayersAvailable
    calibration: OverlayCalibration


# ── Endpoint ────────────────────────────────────────────────────────────


@router.get("/api/v1/clips/{clip_id}/overlays", response_model=ClipOverlayResponse)
async def get_clip_overlays(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ClipOverlayResponse:
    """Aggregate tracklets, events, labels, and metrics for a single clip.

    Player-role users never see suppressed or experimental metrics; staff see
    everything except metrics flagged ``is_suppressed=True``.
    """
    clip_result = await db.execute(select(Clip).where(Clip.id == clip_id))
    clip = clip_result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    calib_result = await db.execute(
        select(FieldCalibration)
        .where(FieldCalibration.video_id == clip.video_id)
        .order_by(FieldCalibration.created_at.desc())
        .limit(1)
    )
    calibration_row = calib_result.scalar_one_or_none()
    if calibration_row is None:
        calibration = OverlayCalibration(
            analytics_safe=False,
            reason=_REASON_TEXT["no_calibration"],
            reason_codes=["no_calibration"],
            confidence=None,
        )
    else:
        codes = list(calibration_row.reason_codes or [])
        calibration = OverlayCalibration(
            analytics_safe=calibration_row.analytics_safe,
            reason=None if calibration_row.analytics_safe else _reason_text(codes),
            reason_codes=codes,
            confidence=calibration_row.confidence,
        )

    tracklets_result = await db.execute(
        select(Tracklet)
        .options(selectinload(Tracklet.track_points))
        .where(Tracklet.clip_id == clip_id)
        .order_by(Tracklet.start_frame)
    )
    tracklets = tracklets_result.scalars().all()

    events_result = await db.execute(
        select(Event).where(Event.clip_id == clip_id).order_by(Event.frame_number)
    )
    events = events_result.scalars().all()

    labels_result = await db.execute(
        select(Label)
        .where(
            (Label.clip_id == clip_id)
            | (Label.tracklet_id.in_(select(Tracklet.id).where(Tracklet.clip_id == clip_id)))
        )
        .order_by(Label.created_at)
    )
    labels = labels_result.scalars().all()

    metrics_q = (
        select(Metric)
        .where(Metric.clip_id == clip_id, Metric.is_suppressed.is_(False))
        .order_by(Metric.metric_name)
    )
    if current_user.role == UserRole.player:
        metrics_q = metrics_q.where(Metric.experimental_flag.is_(False))
    metrics_result = await db.execute(metrics_q)
    metrics = metrics_result.scalars().all()

    return ClipOverlayResponse(
        clip_id=clip_id,
        capture_regime=clip.capture_regime.value if clip.capture_regime else None,
        calibration=calibration,
        tracklets=[
            OverlayTracklet(
                id=t.id,
                player_id=t.player_id,
                start_frame=t.start_frame,
                end_frame=t.end_frame,
                track_confidence=t.track_confidence,
                team_label=t.team_label,
                position_group=t.position_group,
                side_of_ball=t.side_of_ball,
                track_points=[
                    OverlayTrackPoint(
                        frame_number=p.frame_number,
                        field_x=p.field_x,
                        field_y=p.field_y,
                        bbox=p.bbox,
                        detection_confidence=p.detection_confidence,
                    )
                    for p in t.track_points
                ],
            )
            for t in tracklets
        ],
        events=[
            OverlayEvent(
                id=e.id,
                event_type=e.event_type,
                frame_number=e.frame_number,
                timestamp_seconds=e.timestamp_seconds,
                attributes=e.attributes,
            )
            for e in events
        ],
        labels=[
            OverlayLabel(
                id=lb.id,
                tracklet_id=lb.tracklet_id,
                label_type=lb.label_type,
                label_value=lb.label_value,
                source=lb.source,
            )
            for lb in labels
        ],
        metrics=[
            OverlayMetric(
                id=m.id,
                tracklet_id=m.tracklet_id,
                metric_name=m.metric_name,
                metric_value=m.metric_value,
                unit=m.unit,
                confidence=m.confidence,
                effort_zscore=_optional_float(getattr(m, "effort_zscore", None)),
                loaf_flag=_optional_bool(getattr(m, "loaf_flag", None)),
            )
            for m in metrics
        ],
        layers_available=OverlayLayersAvailable(
            tracklets=bool(tracklets),
            events=bool(events),
            labels=bool(labels),
            metrics=bool(metrics),
        ),
    )
