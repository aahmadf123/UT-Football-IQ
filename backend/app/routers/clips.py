"""Clips router — play segmentation, clip CRUD, and boundary corrections."""

import uuid
from typing import Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, require_any_staff, require_coach_or_above
from app.models import (
    CaptureRegime,
    Clip,
    ClipResultState,
    Metric,
    SessionKind,
    SideOfBall,
    User,
    Video,
)

# Recognized clip-level possession values (must match SideOfBall enum).
_VALID_SIDES: frozenset[str] = frozenset(s.value for s in SideOfBall)


def _derive_review_state(clip: Clip) -> str:
    """Map a clip's review/confidence signals onto a single coach-facing state.

    Precedence (Issue #147 — distinguish processed / low-confidence / needs-review):
      ``reviewed``       — a coach has signed off (``is_reviewed``); wins outright.
      ``low_confidence`` — boundary/label confidence at or below the configured
                           threshold, or calibrated high uncertainty (Issue #146).
      ``needs_review``   — a first-pass result nobody has confirmed yet.
    """
    if clip.is_reviewed:
        return "reviewed"
    threshold = get_settings().clip_low_confidence_threshold
    if clip.confidence is not None and clip.confidence < threshold:
        return "low_confidence"
    if (
        clip.uncertainty_calibrated
        and clip.uncertainty_score is not None
        and clip.uncertainty_score > (1.0 - threshold)
    ):
        return "low_confidence"
    return "needs_review"


def _optional_float(value: Any) -> float | None:
    return value if isinstance(value, float) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


log = structlog.get_logger(__name__)
router = APIRouter(tags=["clips"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class ClipCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    start_time: float
    end_time: float
    play_number: int | None = None
    # Coach-tagged play-call code linking this clip to the same-play clip in the
    # other capture regime (Issue #150 cross-regime pairing).
    play_call_id: str | None = None
    label_data: dict[str, Any] | None = None
    confidence: float | None = None
    storage_uri: str | None = None
    boundary_source: str | None = None
    boundary_confidence: float | None = None
    model_version_id: uuid.UUID | None = None
    calibration_version_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    our_possession: SideOfBall | None = None
    side_of_ball: SideOfBall | None = None
    capture_regime: CaptureRegime | None = None
    regime_confidence: float | None = None
    # Active-learning uncertainty written by the GPU worker (Issues #145/#146).
    uncertainty_score: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_calibrated: bool = False
    # Same-session result tier (Issue #147): the GPU worker sets this to
    # ``preliminary`` for same-session first-pass clips, ``final`` for nightly.
    result_state: ClipResultState | None = None


class ClipUpdate(BaseModel):
    start_time: float | None = None
    end_time: float | None = None
    play_number: int | None = None
    # Coach tagging of matched practice <-> game plays (Issue #150).
    play_call_id: str | None = None
    label_data: dict[str, Any] | None = None
    is_reviewed: bool | None = None
    storage_uri: str | None = None
    boundary_source: str | None = None
    boundary_confidence: float | None = None
    our_possession: SideOfBall | None = None
    side_of_ball: SideOfBall | None = None
    capture_regime: CaptureRegime | None = None
    regime_confidence: float | None = None
    uncertainty_score: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_calibrated: bool | None = None
    result_state: ClipResultState | None = None


class ClipResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    video_id: uuid.UUID
    start_time: float
    end_time: float
    play_number: int | None
    play_call_id: str | None
    confidence: float | None
    is_reviewed: bool
    storage_uri: str | None
    label_data: dict[str, Any] | None
    boundary_source: str | None
    boundary_confidence: float | None
    model_version_id: uuid.UUID | None
    calibration_version_id: uuid.UUID | None
    job_id: uuid.UUID | None
    session_kind: SessionKind | None
    our_possession: SideOfBall | None
    side_of_ball: SideOfBall | None
    capture_regime: CaptureRegime | None
    regime_confidence: float | None
    uncertainty_score: float | None
    uncertainty_calibrated: bool
    # Same-session result tier + derived coach-facing states (Issue #147).
    # ``result_state`` is the raw stored value (lenient ``str`` so legacy/unknown
    # rows never fail serialization); ``is_preliminary`` and ``review_state`` are
    # derived for the UI.
    result_state: str | None
    is_preliminary: bool
    review_state: str
    created_at: str

    @classmethod
    def from_orm_clip(cls, c: Clip) -> "ClipResponse":
        return cls(
            id=c.id,
            video_id=c.video_id,
            start_time=c.start_time,
            end_time=c.end_time,
            play_number=c.play_number,
            play_call_id=c.play_call_id,
            confidence=c.confidence,
            is_reviewed=c.is_reviewed,
            storage_uri=c.storage_uri,
            label_data=c.label_data,
            boundary_source=c.boundary_source,
            boundary_confidence=c.boundary_confidence,
            model_version_id=c.model_version_id,
            calibration_version_id=c.calibration_version_id,
            job_id=c.job_id,
            session_kind=c.session_kind,
            our_possession=c.our_possession,
            side_of_ball=c.side_of_ball,
            capture_regime=c.capture_regime,
            regime_confidence=c.regime_confidence,
            uncertainty_score=c.uncertainty_score,
            uncertainty_calibrated=c.uncertainty_calibrated,
            result_state=c.result_state,
            is_preliminary=c.result_state == ClipResultState.preliminary.value,
            review_state=_derive_review_state(c),
            created_at=c.created_at.isoformat(),
        )


class MetricResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    metric_name: str
    metric_value: dict[str, Any]
    unit: str | None
    is_suppressed: bool
    suppression_reason: str | None
    experimental_flag: bool
    analytics_safe: bool
    confidence: float | None
    effort_zscore: float | None
    loaf_flag: bool | None
    evidence_uri: str | None
    clip_id: uuid.UUID
    tracklet_id: uuid.UUID | None
    model_version_id: uuid.UUID | None
    calibration_version_id: uuid.UUID | None
    job_id: uuid.UUID | None
    created_at: str

    @classmethod
    def from_orm_metric(cls, m: Metric) -> "MetricResponse":
        return cls(
            id=m.id,
            metric_name=m.metric_name,
            metric_value=m.metric_value,
            unit=m.unit,
            is_suppressed=m.is_suppressed,
            suppression_reason=m.suppression_reason,
            experimental_flag=m.experimental_flag,
            analytics_safe=m.analytics_safe,
            confidence=m.confidence,
            effort_zscore=_optional_float(getattr(m, "effort_zscore", None)),
            loaf_flag=_optional_bool(getattr(m, "loaf_flag", None)),
            evidence_uri=m.evidence_uri,
            clip_id=m.clip_id,
            tracklet_id=m.tracklet_id,
            model_version_id=m.model_version_id,
            calibration_version_id=m.calibration_version_id,
            job_id=m.job_id,
            created_at=m.created_at.isoformat(),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/v1/videos/{video_id}/clips", response_model=list[ClipResponse])
async def list_clips_for_video(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ClipResponse]:
    """List all clips for a video, ordered by start_time."""
    vid_result = await db.execute(select(Video).where(Video.id == video_id))
    if vid_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    result = await db.execute(
        select(Clip)
        .where(Clip.video_id == video_id)
        .order_by(Clip.start_time)
        .limit(limit)
        .offset(offset)
    )
    return [ClipResponse.from_orm_clip(c) for c in result.scalars().all()]


@router.post(
    "/api/v1/videos/{video_id}/clips",
    response_model=ClipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_clip(
    video_id: uuid.UUID,
    body: ClipCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> ClipResponse:
    """Propose a new play boundary for a video (manual or system-generated)."""
    vid_result = await db.execute(select(Video).where(Video.id == video_id))
    video = vid_result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    if body.start_time >= body.end_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time must be less than end_time",
        )

    # Promote label_data["side_of_ball"] into the typed column when no
    # explicit value was supplied (forward-looking version of the
    # migration's 0010 backfill).
    side_of_ball = body.side_of_ball
    if side_of_ball is None and body.label_data:
        raw = body.label_data.get("side_of_ball")
        if isinstance(raw, str) and raw in _VALID_SIDES:
            side_of_ball = SideOfBall(raw)

    # our_possession defaults to side_of_ball — same vocabulary per ADR §3.
    our_possession = body.our_possession or side_of_ball

    # Game clips must carry their own possession because possession flips
    # within the session. Practice/scrimmage clips may inherit from the
    # parent video.
    if (
        video.session_kind == SessionKind.game
        and our_possession is None
        and video.our_possession is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="our_possession is required for clips of session_kind='game'",
        )

    # Inherit capture-regime from the parent video (set at ingest) unless
    # the caller passed an explicit override. Mirrors the session_kind
    # denormalization above so downstream stages don't need to JOIN videos.
    capture_regime = body.capture_regime or video.capture_regime
    regime_confidence = (
        body.regime_confidence if body.regime_confidence is not None else video.regime_confidence
    )

    clip = Clip(
        id=uuid.uuid4(),
        video_id=video_id,
        start_time=body.start_time,
        end_time=body.end_time,
        play_number=body.play_number,
        play_call_id=body.play_call_id,
        label_data=body.label_data,
        confidence=body.confidence,
        storage_uri=body.storage_uri,
        boundary_source=body.boundary_source,
        boundary_confidence=body.boundary_confidence,
        model_version_id=body.model_version_id,
        calibration_version_id=body.calibration_version_id,
        job_id=body.job_id,
        session_kind=video.session_kind,
        our_possession=our_possession,
        side_of_ball=side_of_ball,
        capture_regime=capture_regime,
        regime_confidence=regime_confidence,
        uncertainty_score=body.uncertainty_score,
        uncertainty_calibrated=body.uncertainty_calibrated,
        result_state=body.result_state.value if body.result_state else None,
    )
    db.add(clip)
    await db.flush()
    log.info(
        "clip_created",
        clip_id=str(clip.id),
        video_id=str(video_id),
        session_kind=str(clip.session_kind) if clip.session_kind else None,
        our_possession=str(clip.our_possession) if clip.our_possession else None,
    )
    return ClipResponse.from_orm_clip(clip)


class ClipFinalizeResponse(BaseModel):
    """Result of upgrading a video's same-session clips to nightly-final."""

    video_id: uuid.UUID
    finalized_count: int


@router.post(
    "/api/v1/videos/{video_id}/clips/finalize",
    response_model=ClipFinalizeResponse,
)
async def finalize_video_clips(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> ClipFinalizeResponse:
    """Upgrade a video's ``preliminary`` clips to ``final`` (Issue #147).

    Called by the GPU worker when nightly full-quality processing finishes for a
    video: the nightly run replaces the same-session first pass in place, so
    every clip still flagged ``preliminary`` flips to ``final`` and the coach's
    "Preliminary" badge clears. Idempotent — clips already ``final`` (or legacy
    NULL) are left untouched, so a re-run finalizes nothing.
    """
    vid_result = await db.execute(select(Video).where(Video.id == video_id))
    if vid_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    result = await db.execute(
        update(Clip)
        .where(
            Clip.video_id == video_id,
            Clip.result_state == ClipResultState.preliminary.value,
        )
        .values(result_state=ClipResultState.final.value)
    )
    # ``execute`` is typed ``Result``; an UPDATE returns a ``CursorResult`` whose
    # ``rowcount`` is the number of clips upgraded.
    finalized = int(cast("CursorResult[Any]", result).rowcount or 0)
    await db.flush()
    log.info("clips_finalized", video_id=str(video_id), finalized_count=finalized)
    return ClipFinalizeResponse(video_id=video_id, finalized_count=finalized)


@router.get("/api/v1/clips/{clip_id}", response_model=ClipResponse)
async def get_clip(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> ClipResponse:
    """Get a single clip by ID."""
    result = await db.execute(select(Clip).where(Clip.id == clip_id))
    clip = result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return ClipResponse.from_orm_clip(clip)


@router.patch("/api/v1/clips/{clip_id}", response_model=ClipResponse)
async def update_clip(
    clip_id: uuid.UUID,
    body: ClipUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ClipResponse:
    """Update a clip's boundaries or review status (coach/analyst override)."""
    result = await db.execute(select(Clip).where(Clip.id == clip_id))
    clip = result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    if body.start_time is not None:
        clip.start_time = body.start_time
    if body.end_time is not None:
        clip.end_time = body.end_time
    if clip.start_time >= clip.end_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time must be less than end_time",
        )
    if body.play_number is not None:
        clip.play_number = body.play_number
    if body.play_call_id is not None:
        clip.play_call_id = body.play_call_id
    if body.label_data is not None:
        clip.label_data = body.label_data
    if body.is_reviewed is not None:
        clip.is_reviewed = body.is_reviewed
        if body.is_reviewed:
            clip.reviewed_by = current_user.id
    if body.storage_uri is not None:
        clip.storage_uri = body.storage_uri
    if body.boundary_source is not None:
        clip.boundary_source = body.boundary_source
    if body.boundary_confidence is not None:
        clip.boundary_confidence = body.boundary_confidence
    if body.our_possession is not None:
        clip.our_possession = body.our_possession
    if body.side_of_ball is not None:
        clip.side_of_ball = body.side_of_ball
    if body.capture_regime is not None:
        # Coach override: treat manual corrections as high-confidence.
        clip.capture_regime = body.capture_regime
        if body.regime_confidence is None:
            clip.regime_confidence = 1.0
    if body.regime_confidence is not None:
        clip.regime_confidence = body.regime_confidence
    if body.uncertainty_score is not None:
        clip.uncertainty_score = body.uncertainty_score
    if body.uncertainty_calibrated is not None:
        clip.uncertainty_calibrated = body.uncertainty_calibrated
    if body.result_state is not None:
        clip.result_state = body.result_state.value

    await db.flush()
    log.info("clip_updated", clip_id=str(clip_id))
    return ClipResponse.from_orm_clip(clip)


@router.get("/api/v1/clips/{clip_id}/metrics", response_model=list[MetricResponse])
async def get_clip_metrics(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> list[MetricResponse]:
    """Get all metrics for a clip — each includes its evidence lineage."""
    clip_result = await db.execute(select(Clip).where(Clip.id == clip_id))
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    result = await db.execute(
        select(Metric).where(Metric.clip_id == clip_id).order_by(Metric.metric_name)
    )
    return [MetricResponse.from_orm_metric(m) for m in result.scalars().all()]


# ``POST /api/v1/metrics`` is owned by ``app.routers.metrics`` (a single handler
# keeps the experimental-flag / analytics-safe ingest policy and role gate in
# one place). It used to be duplicated here, which silently shadowed the
# stricter metrics-router handler because ``clips_router`` is registered first.
