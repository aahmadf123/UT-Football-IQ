"""Clips router — play segmentation, clip CRUD, and boundary corrections."""

import hashlib
import json
import uuid
from typing import Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, require_any_staff, require_coach_or_above
from app.models import (
    CaptureRegime,
    Clip,
    ClipEdit,
    ClipResultState,
    CoachCorrection,
    CorrectionType,
    Metric,
    PlayView,
    ProcessingJob,
    SessionKind,
    SideOfBall,
    User,
    Video,
)
from app.services.annotations import (
    annotation_revision,
    check_revision,
    invalidate_metrics,
    lock_output_id,
    touch_annotation,
    touch_observations,
)
from app.services.clip_state import (
    active_clip,
    layout_is_protected,
    lock_boundary_scope,
    require_active_clip,
)
from app.services.metric_policy import metric_visibility

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


#: Boundaries proposed by the pipeline rather than a coach.
_MACHINE_BOUNDARY_SOURCES = frozenset({"heuristic", "model", "synthetic_test"})


class ClipCreate(BaseModel):
    model_config = {"protected_namespaces": ()}
    id: uuid.UUID | None = None

    start_time: float = Field(ge=0, allow_inf_nan=False)
    end_time: float = Field(gt=0, allow_inf_nan=False)
    play_number: int | None = None
    # Coach-tagged play-call code linking this clip to the same-play clip in the
    # other capture regime (Issue #150 cross-regime pairing).
    play_call_id: str | None = None
    label_data: dict[str, Any] | None = None
    personnel_grouping: str | None = Field(default=None, max_length=20)
    down: int | None = Field(default=None, ge=1, le=4)
    distance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    field_zone: str | None = Field(default=None, max_length=30)
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
    provenance: dict[str, Any] | None = None


class ClipUpdate(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    tracking_review_required: bool | None = None
    start_time: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end_time: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    play_number: int | None = None
    # Coach tagging of matched practice <-> game plays (Issue #150).
    play_call_id: str | None = None
    label_data: dict[str, Any] | None = None
    personnel_grouping: str | None = Field(default=None, max_length=20)
    down: int | None = Field(default=None, ge=1, le=4)
    distance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    field_zone: str | None = Field(default=None, max_length=30)
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
    provenance: dict[str, Any] | None = None


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
    personnel_grouping: str | None = Field(default=None, max_length=20)
    down: int | None = Field(default=None, ge=1, le=4)
    distance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    field_zone: str | None = Field(default=None, max_length=30)
    annotation_revision: int = 0
    tracking_review_required: bool = False
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
    provenance: dict[str, Any] | None = None
    canonical_play_id: uuid.UUID | None = None
    matched_views: list[dict[str, Any]] = Field(default_factory=list)

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
            personnel_grouping=c.personnel_grouping
            if isinstance(c.personnel_grouping, str)
            else None,
            down=c.down if isinstance(c.down, int) else None,
            distance=c.distance if isinstance(c.distance, (float, int)) else None,
            field_zone=c.field_zone if isinstance(c.field_zone, str) else None,
            annotation_revision=annotation_revision(c),
            tracking_review_required=bool(c.label_data.get("tracking_review_required"))
            if isinstance(c.label_data, dict)
            else False,
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
            provenance=c.provenance if isinstance(c.provenance, dict) else None,
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
    provenance: dict[str, Any] | None = None

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
            provenance=m.provenance if isinstance(m.provenance, dict) else None,
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


async def attach_play_context(
    db: AsyncSession, responses: list[ClipResponse]
) -> list[ClipResponse]:
    """Batch the canonical view context, including independent recordings."""
    if not responses:
        return responses
    views = (
        (
            await db.execute(
                select(PlayView).where(
                    PlayView.clip_id.in_([response.id for response in responses])
                )
            )
        )
        .scalars()
        .all()
    )
    by_clip = {view.clip_id: view.play_id for view in views if isinstance(view, PlayView)}
    matches: dict[uuid.UUID, list[dict[str, Any]]] = {}
    if by_clip:
        rows = (
            await db.execute(
                select(PlayView, Clip, Video)
                .join(Clip, Clip.id == PlayView.clip_id)
                .join(Video, Video.id == Clip.video_id)
                .where(PlayView.play_id.in_(set(by_clip.values())))
                .where(active_clip())
                .order_by(Clip.created_at)
            )
        ).all()
        for view, clip, video in rows:
            matches.setdefault(view.play_id, []).append(
                {
                    "clip_id": str(clip.id),
                    "video_id": str(video.id),
                    "camera_label": video.camera_label,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "alignment_offset_seconds": view.alignment_offset_seconds,
                    "alignment_confidence": view.alignment_confidence,
                    "match_source": view.match_source,
                }
            )
    for response in responses:
        response.canonical_play_id = by_clip.get(response.id, response.id)
        response.matched_views = matches.get(response.canonical_play_id, [])
    return responses


@router.get("/api/v1/videos/{video_id}/clips", response_model=list[ClipResponse])
async def list_clips_for_video(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
        .where(Clip.video_id == video_id, active_clip())
        .order_by(Clip.start_time)
        .limit(limit)
        .offset(offset)
    )
    return await attach_play_context(
        db, [ClipResponse.from_orm_clip(c) for c in result.scalars().all()]
    )


@router.post(
    "/api/v1/videos/{video_id}/clips",
    response_model=ClipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_clip(
    video_id: uuid.UUID,
    body: ClipCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> ClipResponse:
    """Propose a new play boundary for a video (manual or system-generated)."""
    machine_cut = body.boundary_source in _MACHINE_BOUNDARY_SOURCES or body.job_id is not None
    if machine_cut:
        await lock_boundary_scope(db, video_id)
    if body.id is not None:
        await lock_output_id(db, body.id)
        existing = (await db.execute(select(Clip).where(Clip.id == body.id))).scalar_one_or_none()
        if existing is not None:
            if existing.video_id != video_id:
                raise HTTPException(409, detail="Output ID belongs to another recording")
            return ClipResponse.from_orm_clip(existing)
    vid_result = await db.execute(select(Video).where(Video.id == video_id))
    video = vid_result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    if machine_cut:
        manual = (
            await db.execute(
                select(Clip.id)
                .where(Clip.video_id == video_id, active_clip(), Clip.boundary_source == "manual")
                .limit(1)
            )
        ).scalar_one_or_none()
        edited = (
            await db.execute(select(ClipEdit.id).where(ClipEdit.video_id == video_id).limit(1))
        ).scalar_one_or_none()
        if manual is not None or edited is not None:
            raise HTTPException(
                409,
                detail=(
                    "Staff boundary layout changed; reload boundary-overrides "
                    "and reuse its clip IDs"
                ),
            )
    if body.start_time >= body.end_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time must be less than end_time",
        )
    if isinstance(video.duration_seconds, (float, int)) and body.end_time > video.duration_seconds:
        raise HTTPException(422, detail="Clip boundary exceeds recording duration")

    clip = _build_clip(video_id, body, video, machine_cut)
    db.add(clip)
    await db.flush()
    if isinstance(video.practice_session_id, uuid.UUID):
        from app.routers.sessions import ensure_recording_plays

        await ensure_recording_plays(db, video)
    log.info(
        "clip_created",
        clip_id=str(clip.id),
        video_id=str(video_id),
        session_kind=str(clip.session_kind) if clip.session_kind else None,
        our_possession=str(clip.our_possession) if clip.our_possession else None,
    )
    return ClipResponse.from_orm_clip(clip)


def _build_clip(video_id: uuid.UUID, body: ClipCreate, video: Video, machine_cut: bool) -> Clip:
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
    # parent video. The exception is film the pipeline cut automatically: it
    # can't know possession yet, so the clip is marked pending and filled in
    # by the CFBD play-by-play link (app.cfbd.game_link) or a coach.
    if (
        video.session_kind == SessionKind.game
        and our_possession is None
        and video.our_possession is None
    ):
        if not machine_cut:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="our_possession is required for clips of session_kind='game'",
            )
        body.label_data = {**(body.label_data or {}), "possession_pending": True}

    # Inherit capture-regime from the parent video (set at ingest) unless
    # the caller passed an explicit override. Mirrors the session_kind
    # denormalization above so downstream stages don't need to JOIN videos.
    capture_regime = body.capture_regime or video.capture_regime
    regime_confidence = (
        body.regime_confidence if body.regime_confidence is not None else video.regime_confidence
    )

    clip = Clip(
        id=body.id or uuid.uuid4(),
        video_id=video_id,
        start_time=body.start_time,
        end_time=body.end_time,
        play_number=body.play_number,
        play_call_id=body.play_call_id,
        label_data=body.label_data,
        personnel_grouping=body.personnel_grouping,
        down=body.down,
        distance=body.distance,
        field_zone=body.field_zone,
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
        result_state=ClipResultState.preliminary.value
        if machine_cut
        else body.result_state.value
        if body.result_state
        else None,
        provenance={**(body.provenance or {}), "cuts_state": "ready"}
        if machine_cut
        else body.provenance,
    )
    return clip


class ClipBatchCreate(BaseModel):
    generation_id: str = Field(min_length=1, max_length=128)
    expected_active_clip_ids: list[uuid.UUID] = Field(max_length=10000)
    clips: list[ClipCreate] = Field(min_length=1, max_length=10000)


@router.post("/api/v1/videos/{video_id}/clips/batch")
async def create_clip_batch(
    video_id: uuid.UUID,
    body: ClipBatchCreate,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    """Publish a complete cut layout in one transaction; retain every original.

    Cut readiness is independent of later specialist analysis. A protected prior
    layout is never recut automatically, so complete results keep their IDs.
    """
    await lock_boundary_scope(db, video_id)
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Video not found")
    job_header = request.headers.get("x-job-id")
    if job_header:
        job = await db.get(ProcessingJob, uuid.UUID(job_header))
        if job is None or job.video_id != video_id or any(c.job_id != job.id for c in body.clips):
            raise HTTPException(409, "Worker result does not belong to this recording/attempt")
    manifest = hashlib.sha256(
        json.dumps(
            [clip.model_dump(mode="json") for clip in body.clips],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    historical = list(
        (
            await db.execute(
                select(Clip)
                .where(
                    Clip.video_id == video_id,
                    Clip.provenance["segmentation_generation"].astext == body.generation_id,
                )
                .order_by(Clip.start_time, Clip.id)
            )
        )
        .scalars()
        .all()
    )
    if historical:
        if len(historical) != len(body.clips) or any(
            (c.provenance or {}).get("segmentation_manifest") != manifest
            or (c.provenance or {}).get("state") == "superseded"
            for c in historical
        ):
            raise HTTPException(409, "Segmentation generation is stale or has conflicting content")
        return {
            "generation_id": body.generation_id,
            "clips": [
                ClipResponse.from_orm_clip(c).model_dump(mode="json")
                for c in sorted(
                    historical, key=lambda c: (c.provenance or {}).get("segmentation_index", 0)
                )
            ],
        }
    active = list(
        (
            await db.execute(
                select(Clip)
                .where(
                    Clip.video_id == video_id,
                    active_clip(),
                )
                .order_by(Clip.start_time, Clip.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if len(set(body.expected_active_clip_ids)) != len(body.expected_active_clip_ids) or {
        c.id for c in active
    } != set(body.expected_active_clip_ids):
        raise HTTPException(409, "Recording layout changed; reload its active clips")
    windows = [(c.start_time, c.end_time) for c in body.clips]
    if len(set(windows)) != len(windows):
        raise HTTPException(422, "Duplicate source windows in proposed layout")
    supplied_ids = [clip.id for clip in body.clips if clip.id is not None]
    if len(set(supplied_ids)) != len(supplied_ids):
        raise HTTPException(422, "Duplicate output IDs in proposed layout")
    if not video.duration_seconds or video.duration_seconds <= 0:
        raise HTTPException(422, "A verified source duration is required before replacing cuts")
    # Context/action windows may overlap. Their union must retain the complete
    # source so an omitted setup, tail, aborted snap or uncertain shot is not lost.
    tolerance = min(0.1, 1 / video.fps) if video.fps and video.fps > 0 else 0.04
    covered_until = 0.0
    for start, end in sorted(windows):
        if start > covered_until + tolerance:
            raise HTTPException(422, "Proposed layout leaves an uncovered source interval")
        covered_until = max(covered_until, end)
    if covered_until < video.duration_seconds - tolerance:
        raise HTTPException(422, "Proposed layout omits the source tail")
    for index, clip in enumerate(body.clips):
        if clip.boundary_source not in _MACHINE_BOUNDARY_SOURCES:
            raise HTTPException(422, "Batch publication is for machine cuts only")
        if clip.start_time >= clip.end_time or (
            video.duration_seconds is not None and clip.end_time > video.duration_seconds
        ):
            raise HTTPException(422, "Invalid or out-of-recording clip boundary")
        if clip.id is None:
            clip.id = uuid.uuid5(video_id, f"{body.generation_id}:{index}")
        existing = await db.get(Clip, clip.id)
        if existing is not None:
            raise HTTPException(409, "Proposed output ID already belongs to an existing clip")
    if len({clip.id for clip in body.clips}) != len(body.clips):
        raise HTTPException(422, "Duplicate output IDs in proposed layout")
    if active and sorted(windows) == sorted((c.start_time, c.end_time) for c in active):
        # Preserve result/identity/correction bindings when only processing changed.
        return {
            "generation_id": body.generation_id,
            "clips": [ClipResponse.from_orm_clip(c).model_dump(mode="json") for c in active],
        }
    if await layout_is_protected(db, video_id, active):
        raise HTTPException(
            409, "Staff or published results protect this layout; reuse boundary-overrides"
        )
    # A savepoint also makes atomicity explicit for callers with an outer
    # transaction: any insert/grouping failure restores the entire old layout.
    async with db.begin_nested():
        for old in active:
            old.provenance = {
                **(old.provenance or {}),
                "state": "superseded",
                "superseded_by_segmentation": body.generation_id,
                "replacement_clip_ids": [str(clip.id) for clip in body.clips],
            }
        new_clips = []
        for index, clip in enumerate(body.clips):
            clip.provenance = {
                **(clip.provenance or {}),
                "segmentation_generation": body.generation_id,
                "segmentation_manifest": manifest,
                "segmentation_index": index,
                "state": "current",
            }
            record = _build_clip(video_id, clip, video, True)
            db.add(record)
            new_clips.append(record)
        await db.flush()
        if video.practice_session_id:
            from app.routers.sessions import ensure_recording_plays

            await ensure_recording_plays(db, video)
        published = [ClipResponse.from_orm_clip(clip).model_dump(mode="json") for clip in new_clips]
    return {"generation_id": body.generation_id, "clips": published}


class ClipFinalizeResponse(BaseModel):
    """Cut readiness; result tiers are promoted with complete analysis."""

    video_id: uuid.UUID
    finalized_count: int
    cuts_ready_count: int = 0


@router.post(
    "/api/v1/videos/{video_id}/clips/finalize",
    response_model=ClipFinalizeResponse,
)
async def finalize_video_clips(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> ClipFinalizeResponse:
    """Record completed cuts without prematurely promoting pending analysis."""
    vid_result = await db.execute(select(Video).where(Video.id == video_id))
    if vid_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    result = await db.execute(
        update(Clip)
        .where(
            Clip.video_id == video_id,
            active_clip(),
        )
        .values(
            provenance=func.coalesce(Clip.provenance, sql_cast({}, JSONB)).op("||")(
                sql_cast({"cuts_state": "ready"}, JSONB)
            )
        )
    )
    # ``execute`` is typed ``Result``; an UPDATE returns a ``CursorResult`` whose
    # ``rowcount`` is the number of cuts marked ready.
    finalized = int(cast("CursorResult[Any]", result).rowcount or 0)
    await db.flush()
    log.info("clip_cuts_ready", video_id=str(video_id), cuts_ready_count=finalized)
    return ClipFinalizeResponse(video_id=video_id, finalized_count=0, cuts_ready_count=finalized)


@router.get("/api/v1/clips/{clip_id}", response_model=ClipResponse)
async def get_clip(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> ClipResponse:
    """Get a single clip by ID."""
    result = await db.execute(select(Clip).where(Clip.id == clip_id))
    clip = result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return (await attach_play_context(db, [ClipResponse.from_orm_clip(clip)]))[0]


@router.patch("/api/v1/clips/{clip_id}", response_model=ClipResponse)
async def update_clip(
    clip_id: uuid.UUID,
    body: ClipUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ClipResponse:
    """Update a clip's boundaries or review status (coach/analyst override)."""
    if body.start_time is not None or body.end_time is not None:
        await lock_boundary_scope(db, clip_id)
    result = await db.execute(select(Clip).where(Clip.id == clip_id).with_for_update())
    clip = result.scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    check_revision(clip, body.expected_revision)
    require_active_clip(clip)
    before = {"start_seconds": clip.start_time, "end_seconds": clip.end_time}
    metadata_fields = (
        "personnel_grouping",
        "down",
        "distance",
        "field_zone",
        "play_number",
        "play_call_id",
        "our_possession",
        "side_of_ball",
        "capture_regime",
        "regime_confidence",
    )
    metadata_before = {
        key: str(value) if isinstance(value := getattr(clip, key), uuid.UUID) else value
        for key in metadata_fields
    }

    if body.start_time is not None:
        clip.start_time = body.start_time
    if body.end_time is not None:
        clip.end_time = body.end_time
    if clip.start_time >= clip.end_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time must be less than end_time",
        )
    if body.start_time is not None or body.end_time is not None:
        video = await db.get(Video, clip.video_id)
        if (
            video is not None
            and isinstance(video.duration_seconds, (float, int))
            and clip.end_time > video.duration_seconds
        ):
            raise HTTPException(422, detail="Clip boundary exceeds recording duration")
    if body.play_number is not None:
        clip.play_number = body.play_number
    if body.play_call_id is not None:
        clip.play_call_id = body.play_call_id
    if body.label_data is not None:
        current = clip.label_data if isinstance(clip.label_data, dict) else {}
        clip.label_data = {
            **current,
            **{k: v for k, v in body.label_data.items() if not k.startswith("_")},
        }
    metadata_updates = {
        key: getattr(body, key) for key in metadata_fields if key in body.model_fields_set
    }
    for key, value in metadata_updates.items():
        setattr(clip, key, value)
    if body.is_reviewed is not None:
        clip.is_reviewed = body.is_reviewed
        if body.is_reviewed:
            clip.reviewed_by = current_user.id
        else:
            clip.reviewed_by = None
    if body.storage_uri is not None:
        clip.storage_uri = body.storage_uri
    if body.boundary_source is not None:
        clip.boundary_source = body.boundary_source
    if body.boundary_confidence is not None:
        clip.boundary_confidence = body.boundary_confidence
    if body.our_possession is not None:
        clip.our_possession = body.our_possession
        if isinstance(clip.label_data, dict) and "possession_pending" in clip.label_data:
            clip.label_data = {
                key: value for key, value in clip.label_data.items() if key != "possession_pending"
            }
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
    if body.provenance is not None:
        clip.provenance = body.provenance
    if body.tracking_review_required is not None:
        data = clip.label_data if isinstance(clip.label_data, dict) else {}
        clip.label_data = {**data, "tracking_review_required": body.tracking_review_required}

    boundary_changed = body.start_time is not None or body.end_time is not None
    annotations_changed = (
        boundary_changed
        or bool(metadata_updates)
        or body.label_data is not None
        or body.tracking_review_required is not None
    )
    if annotations_changed:
        if (
            boundary_changed
            or body.tracking_review_required is not None
            or {"capture_regime", "side_of_ball", "our_possession"}.intersection(metadata_updates)
        ):
            touch_observations(clip)
        revision = touch_annotation(clip)
        if body.is_reviewed is None:
            clip.is_reviewed = False
            clip.reviewed_by = None
        if boundary_changed:
            clip.boundary_source = "manual"
            clip.boundary_confidence = 1.0
            clip.storage_uri = None
        await invalidate_metrics(db, clip, "coach_annotation_changed")
        db.add(
            CoachCorrection(
                id=uuid.uuid4(),
                clip_id=clip.id,
                correction_type=CorrectionType.clip_boundary
                if boundary_changed
                else CorrectionType.personnel_tag,
                original_value=before if boundary_changed else metadata_before,
                corrected_value={
                    "start_seconds": clip.start_time,
                    "end_seconds": clip.end_time,
                    "_revision": revision,
                }
                if boundary_changed
                else {
                    "metadata": {
                        key: str(value) if isinstance(value, uuid.UUID) else value
                        for key, value in metadata_updates.items()
                    },
                    "label_data": body.label_data,
                    "_revision": revision,
                },
                corrected_by=current_user.id,
                training_eligible=boundary_changed or "personnel_grouping" in metadata_updates,
            )
        )
    await db.flush()
    log.info("clip_updated", clip_id=str(clip_id))
    return ClipResponse.from_orm_clip(clip)


@router.get("/api/v1/clips/{clip_id}/metrics", response_model=list[MetricResponse])
async def get_clip_metrics(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[MetricResponse]:
    """Get all metrics for a clip — each includes its evidence lineage."""
    clip_result = await db.execute(select(Clip).where(Clip.id == clip_id))
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    result = await db.execute(
        metric_visibility(select(Metric).where(Metric.clip_id == clip_id), current_user).order_by(
            Metric.metric_name
        )
    )
    return [MetricResponse.from_orm_metric(m) for m in result.scalars().all()]


# ``POST /api/v1/metrics`` is owned by ``app.routers.metrics`` (a single handler
# keeps the experimental-flag / analytics-safe ingest policy and role gate in
# one place). It used to be duplicated here, which silently shadowed the
# stricter metrics-router handler because ``clips_router`` is registered first.
