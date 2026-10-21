"""Metrics router — computed analytics metrics with head-orientation support.

Head orientation metrics (headdirectionyaw, headturnlatency, progressionreadorder,
staredownflag, playactionresponse) are stored with experimental_flag=True and
analytics_safe=False by default.  A position coach must call the review endpoint
before they appear in staff-facing views.  Player-facing views must never see
experimental metrics regardless of analytics_safe.

Valid metric names for head-orientation:
  - headdirectionyaw       — yaw angle (degrees) per frame relative to field direction
  - headturnlatency        — time (seconds) from mesh point to correct run/pass read
  - progressionreadorder   — ordered list of receiver reads before release
  - staredownflag          — bool: no head movement between snap and throw
  - playactionresponse     — bool/latency: head-turn after play fake
"""

import uuid
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_analyst_or_above, require_coach_or_above
from app.models import (
    Clip,
    HeadOrientationReview,
    Metric,
    ProcessingJob,
    Tracklet,
    User,
    UserRole,
)
from app.services.annotations import lock_output_id
from app.services.clip_state import lock_active_clip, require_active_clip
from app.services.metric_definitions import DEFINITION_VERSION, metric_definitions
from app.services.metric_eligibility import automatic_exclusion
from app.services.metric_policy import INVALID_SOURCES, metric_visibility, metric_visible
from app.services.metric_publication import trusted_metric_job

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])

# ── Head orientation metric name constants ─────────────────────────────────────

HEAD_ORIENTATION_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "headdirectionyaw",
        "headturnlatency",
        "progressionreadorder",
        "staredownflag",
        "playactionresponse",
    }
)

# Frontier analytics (Issue #10): xSep / xYards / xPressure. These are
# experimental scaffolds — derived from tracking/pose/SAM outputs that are not
# yet validated for Toledo — so they are forced experimental and never
# analytics_safe on ingest. A coach must review them before they leave the
# experimental surface, exactly like head-orientation metrics.
FRONTIER_METRIC_NAMES: frozenset[str] = frozenset({"xsep", "xyards", "xpressure"})
EXPECTED_OUTCOME_METRIC_NAMES: frozenset[str] = frozenset(
    {"expected_completion", "expected_rushing_gain", "expected_yac", "contextual_pressure"}
)
REVIEW_CANDIDATE_METRIC_NAMES: frozenset[str] = frozenset(
    {"effort_review_candidate", "pose_body_orientation_proxy", "workload_fusion"}
)

# Metrics that belong exclusively to the health-workload RBAC surface and must
# be accessed via /api/v1/health-workload/* endpoints.  The generic GET
# /metrics read path does not enforce the health-workload policy, so these
# names are excluded from it entirely to prevent bypassing the sports-
# performance-only gate added in Issue #149.
HEALTH_WORKLOAD_ONLY_METRIC_NAMES: frozenset[str] = frozenset({"workload_fusion"})

# ── Schemas ───────────────────────────────────────────────────────────────────


class MetricCreate(BaseModel):
    model_config = {"protected_namespaces": ()}
    id: uuid.UUID | None = None

    clip_id: uuid.UUID
    tracklet_id: uuid.UUID | None = None
    metric_name: str = Field(..., max_length=255)
    metric_value: dict[str, Any]
    unit: str | None = Field(default=None, max_length=50)
    experimental_flag: bool = False
    analytics_safe: bool = False
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    effort_zscore: float | None = None
    loaf_flag: bool | None = None
    sprint_count: int | None = Field(default=None, ge=0)
    asymmetry_index: float | None = Field(default=None, ge=0.0)
    injury_risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_uri: str | None = None
    model_version_id: uuid.UUID | None = None
    calibration_version_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    is_suppressed: bool = False
    suppression_reason: str | None = None
    provenance: dict[str, Any] | None = None


class MetricResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    clip_id: uuid.UUID
    tracklet_id: uuid.UUID | None
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
    sprint_count: int | None
    asymmetry_index: float | None
    injury_risk_score: float | None
    evidence_uri: str | None
    model_version_id: uuid.UUID | None
    calibration_version_id: uuid.UUID | None
    job_id: uuid.UUID | None
    created_at: str
    provenance: dict[str, Any] | None = None

    @classmethod
    def from_orm_metric(cls, m: Metric) -> "MetricResponse":
        return cls(
            id=m.id,
            clip_id=m.clip_id,
            tracklet_id=m.tracklet_id,
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
            sprint_count=_optional_int(getattr(m, "sprint_count", None)),
            asymmetry_index=_optional_float(getattr(m, "asymmetry_index", None)),
            injury_risk_score=_optional_float(getattr(m, "injury_risk_score", None)),
            evidence_uri=m.evidence_uri,
            model_version_id=m.model_version_id,
            calibration_version_id=m.calibration_version_id,
            job_id=m.job_id,
            created_at=m.created_at.isoformat(),
            provenance=m.provenance if isinstance(m.provenance, dict) else None,
        )


class ReviewCreate(BaseModel):
    review_action: Literal["approve", "reject", "flag"]
    position_group: str | None = Field(default=None, max_length=50)
    notes: str | None = None


class ReviewResponse(BaseModel):
    id: uuid.UUID
    metric_id: uuid.UUID
    reviewer_id: uuid.UUID
    review_action: str
    position_group: str | None
    notes: str | None
    created_at: str

    @classmethod
    def from_orm_review(cls, r: HeadOrientationReview) -> "ReviewResponse":
        return cls(
            id=r.id,
            metric_id=r.metric_id,
            reviewer_id=r.reviewer_id,
            review_action=r.review_action,
            position_group=r.position_group,
            notes=r.notes,
            created_at=r.created_at.isoformat(),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_player_role(user: User) -> bool:
    return user.role == UserRole.player


def _optional_float(value: Any) -> float | None:
    return value if isinstance(value, float) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=MetricResponse, status_code=status.HTTP_201_CREATED)
async def create_metric(
    body: MetricCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
    producer_job: Annotated[ProcessingJob | None, Depends(trusted_metric_job)] = None,
) -> MetricResponse:
    """Store a computed metric for a clip.

    Head-orientation metric names (headdirectionyaw, headturnlatency,
    progressionreadorder, staredownflag, playactionresponse) are automatically
    marked experimental_flag=True regardless of the payload value.
    """
    if body.id is not None:
        await lock_output_id(db, body.id)
        prior = (await db.execute(select(Metric).where(Metric.id == body.id))).scalar_one_or_none()
        if prior is not None:
            if (
                prior.clip_id != body.clip_id
                or prior.tracklet_id != body.tracklet_id
                or prior.metric_name != body.metric_name
            ):
                raise HTTPException(409, detail="Output ID belongs to another measurement")
            if body.job_id is not None and prior.job_id != body.job_id:
                raise HTTPException(409, detail="Output ID belongs to another result version")
            return MetricResponse.from_orm_metric(prior)
    clip_result = await db.execute(select(Clip).where(Clip.id == body.clip_id).with_for_update())
    target_clip = clip_result.scalar_one_or_none()
    if target_clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    require_active_clip(target_clip)

    if body.tracklet_id is not None:
        t_result = await db.execute(select(Tracklet).where(Tracklet.id == body.tracklet_id))
        target_tracklet = t_result.scalar_one_or_none()
        if target_tracklet is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracklet not found")
        if target_tracklet.clip_id != body.clip_id:
            raise HTTPException(422, detail="Tracklet does not belong to this clip")

    # Head-orientation and frontier-analytics metrics are always experimental
    # until coach-approved, regardless of the payload.
    name_lower = body.metric_name.lower()
    is_always_experimental = (
        body.metric_name in HEAD_ORIENTATION_METRIC_NAMES
        or name_lower in FRONTIER_METRIC_NAMES
        or name_lower in REVIEW_CANDIDATE_METRIC_NAMES
    )
    provenance = body.provenance or body.metric_value.get("provenance", {})
    provenance = provenance if isinstance(provenance, dict) else {}
    source = provenance.get("source") if isinstance(provenance, dict) else None
    invalid_source = source in INVALID_SOURCES
    effective_experimental = (
        body.experimental_flag
        or is_always_experimental
        or source == "heuristic"
        or invalid_source
        or (isinstance(provenance, dict) and provenance.get("validated") is False)
    )
    # analytics_safe must not be pre-set true for these metrics without review
    effective_analytics_safe = (
        body.analytics_safe
        and not is_always_experimental
        and not invalid_source
        and (
            source in {"manual", "human"}
            or (isinstance(provenance, dict) and provenance.get("validated") is True)
        )
    )
    if (
        effective_analytics_safe
        and source == "real"
        and name_lower not in EXPECTED_OUTCOME_METRIC_NAMES
    ):
        reason = await automatic_exclusion(
            db,
            clip=target_clip,
            name=body.metric_name,
            value=body.metric_value,
            provenance=provenance,
            model_id=body.model_version_id,
        )
        if reason:
            effective_analytics_safe = False
            provenance = {**provenance, "eligibility": "excluded", "exclusion_reason": reason}

    if name_lower in EXPECTED_OUTCOME_METRIC_NAMES:
        reason = "expected_outcome_model_approval_required"
        if source == "real" and body.experimental_flag is False and body.analytics_safe is True:
            reason = await automatic_exclusion(
                db,
                clip=target_clip,
                name=name_lower,
                value=body.metric_value,
                provenance=provenance,
                model_id=body.model_version_id,
                tracklet_id=body.tracklet_id,
            )
        effective_experimental = reason is not None
        effective_analytics_safe = reason is None
        if reason:
            provenance = {**provenance, "eligibility": "excluded", "exclusion_reason": reason}

    if source == "real" and effective_analytics_safe:
        if (
            producer_job is None
            or body.job_id != producer_job.id
            or producer_job.video_id != target_clip.video_id
            or (producer_job.clip_id is not None and producer_job.clip_id != target_clip.id)
        ):
            raise HTTPException(
                403, detail="Automatic metric publication requires its trusted pipeline job"
            )
        provenance = {
            **provenance,
            "producer": {
                "job_id": str(producer_job.id),
                "worker_id": producer_job.leased_by,
                "attempt_count": producer_job.attempt_count,
                "principal_id": str(_current_user.id),
            },
        }

    if body.job_id is not None:
        provenance = {**provenance, "publication_state": "pending"}

    metric = Metric(
        id=body.id or uuid.uuid4(),
        clip_id=body.clip_id,
        tracklet_id=body.tracklet_id,
        metric_name=body.metric_name,
        metric_value=body.metric_value,
        unit=body.unit,
        experimental_flag=effective_experimental,
        analytics_safe=effective_analytics_safe,
        confidence=body.confidence,
        effort_zscore=body.effort_zscore,
        loaf_flag=body.loaf_flag,
        sprint_count=body.sprint_count,
        asymmetry_index=body.asymmetry_index,
        injury_risk_score=body.injury_risk_score,
        evidence_uri=body.evidence_uri,
        model_version_id=body.model_version_id,
        calibration_version_id=body.calibration_version_id,
        job_id=body.job_id,
        is_suppressed=body.is_suppressed or invalid_source,
        suppression_reason="invalid_output_source" if invalid_source else body.suppression_reason,
        provenance=provenance or None,
    )
    db.add(metric)
    await db.flush()
    log.info(
        "metric_created",
        metric_id=str(metric.id),
        metric_name=body.metric_name,
        clip_id=str(body.clip_id),
        experimental=effective_experimental,
    )
    return MetricResponse.from_orm_metric(metric)


@router.get("/definitions")
async def definitions(
    _current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return {"definition_version": DEFINITION_VERSION, "definitions": metric_definitions()}


@router.get("", response_model=list[MetricResponse])
async def list_metrics(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
    clip_id: uuid.UUID | None = Query(default=None),
    tracklet_id: uuid.UUID | None = Query(default=None),
    metric_name: str | None = Query(default=None),
    experimental: bool | None = Query(default=None),
    analytics_safe: bool | None = Query(default=None),
    player_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[MetricResponse]:
    """List metrics with optional filters.

    Players and viewers with player role never see experimental metrics.
    """
    q = (
        metric_visibility(select(Metric), current_user)
        .order_by(Metric.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    # Health-workload metrics are only accessible via the /health-workload
    # surface which enforces full RBAC; exclude them from this endpoint.
    q = q.where(~Metric.metric_name.in_(HEALTH_WORKLOAD_ONLY_METRIC_NAMES))

    if clip_id is not None:
        q = q.where(Metric.clip_id == clip_id)
    if tracklet_id is not None:
        q = q.where(Metric.tracklet_id == tracklet_id)
    if metric_name is not None:
        q = q.where(Metric.metric_name == metric_name)
    if experimental is not None:
        q = q.where(Metric.experimental_flag == experimental)
    if analytics_safe is not None:
        q = q.where(Metric.analytics_safe == analytics_safe)
    if player_id is not None:
        q = q.where(Metric.metric_value["player_id"].as_string() == str(player_id))

    # Players must never see experimental metrics regardless of analytics_safe
    if _is_player_role(current_user):
        q = q.where(Metric.experimental_flag.is_(False))

    result = await db.execute(q)
    return [MetricResponse.from_orm_metric(m) for m in result.scalars().all()]


@router.get("/{metric_id}", response_model=MetricResponse)
async def get_metric(
    metric_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
) -> MetricResponse:
    """Get a single metric by ID."""
    result = await db.execute(select(Metric).where(Metric.id == metric_id))
    metric = result.scalar_one_or_none()
    if metric is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found")

    if not metric_visible(metric, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found")

    return MetricResponse.from_orm_metric(metric)


@router.post(
    "/{metric_id}/reviews",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def review_metric(
    metric_id: uuid.UUID,
    body: ReviewCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ReviewResponse:
    """Position-coach review of an experimental head-orientation metric.

    - approve: sets metric.analytics_safe=True — metric becomes visible to staff
    - reject:  metric stays suppressed; model team notified via log
    - flag:    metric routed to model review queue

    Only experimental metrics can be reviewed via this endpoint.
    """
    result = await db.execute(select(Metric).where(Metric.id == metric_id))
    metric = result.scalar_one_or_none()
    if metric is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found")
    await lock_active_clip(db, metric.clip_id)

    if not metric.experimental_flag:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only experimental metrics require a position-coach review",
        )

    if (
        metric.metric_name.lower() in EXPECTED_OUTCOME_METRIC_NAMES
        and body.review_action == "approve"
    ):
        raise HTTPException(
            422, detail="Predictions require held-out model evaluation and human model promotion."
        )

    review = HeadOrientationReview(
        id=uuid.uuid4(),
        metric_id=metric.id,
        reviewer_id=current_user.id,
        review_action=body.review_action,
        position_group=body.position_group,
        notes=body.notes,
    )
    db.add(review)

    if body.review_action == "approve":
        provenance = metric.provenance or (metric.metric_value or {}).get("provenance", {})
        if isinstance(provenance, dict) and provenance.get("source") in INVALID_SOURCES:
            raise HTTPException(
                422, detail="Synthetic or unavailable measurements cannot be approved"
            )
        if not isinstance(provenance, dict) or provenance.get("source") not in {
            "real",
            "manual",
            "human",
        }:
            raise HTTPException(422, detail="Measurement provenance is required before approval")
        if metric.is_suppressed:
            raise HTTPException(
                422, detail="Resolve the measurement's suppression reason before approval"
            )
        metric.analytics_safe = True
        log.info(
            "head_orientation_metric_approved",
            metric_id=str(metric_id),
            reviewer_id=str(current_user.id),
            position_group=body.position_group,
        )
    elif body.review_action == "reject":
        metric.analytics_safe = False
        log.info(
            "head_orientation_metric_rejected",
            metric_id=str(metric_id),
            reviewer_id=str(current_user.id),
        )
    else:  # flag
        log.info(
            "head_orientation_metric_flagged_for_model_review",
            metric_id=str(metric_id),
            reviewer_id=str(current_user.id),
        )

    await db.flush()
    return ReviewResponse.from_orm_review(review)


@router.get("/{metric_id}/reviews", response_model=list[ReviewResponse])
async def list_metric_reviews(
    metric_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
) -> list[ReviewResponse]:
    """List all position-coach reviews for a metric."""
    metric_result = await db.execute(select(Metric).where(Metric.id == metric_id))
    if metric_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found")

    result = await db.execute(
        select(HeadOrientationReview)
        .where(HeadOrientationReview.metric_id == metric_id)
        .order_by(HeadOrientationReview.created_at.desc())
    )
    return [ReviewResponse.from_orm_review(r) for r in result.scalars().all()]
