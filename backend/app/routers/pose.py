"""Pose-Lite Biomechanics router (Issue #6).

Endpoints for raw pose keypoints, pending biomechanics metric review, and
stride asymmetry trends.

All pose-derived biomechanics metrics are stored with ``experimental_flag=True``
and ``analytics_safe=False``.  They are surfaced here for position-coach review
only.  Once a coach approves a metric via ``POST /metrics/{id}/reviews``, it
becomes ``analytics_safe=True`` and visible in staff dashboards.

Player-facing views must NEVER show pose metrics, regardless of approval status.

Pose metric names (prefixed with ``pose_``):
  pose_pad_level
  pose_block_shed_timing
  pose_pass_set_weight_distribution
  pose_wr_shoulder_over_knee
  pose_wr_deceleration_steps
  pose_wr_hip_sink_depth
  pose_wr_pre_snap_stance
  pose_wr_release_technique
  pose_qb_stride_consistency
  pose_qb_shoulder_hip_separation
  pose_qb_weight_transfer
  pose_qb_release_consistency
  pose_stride_symmetry
  pose_biomechanical_drift
  pose_body_orientation_proxy
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_coach_or_above, require_sportsperformance_or_above
from app.models import (
    HeadOrientationReview,
    Metric,
    PoseKeypoints,
    Tracklet,
    User,
    UserRole,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/pose", tags=["pose-biomechanics"])

# ── All pose metric names ──────────────────────────────────────────────────────
POSE_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "pose_pad_level",
        "pose_block_shed_timing",
        "pose_pass_set_weight_distribution",
        "pose_wr_shoulder_over_knee",
        "pose_wr_deceleration_steps",
        "pose_wr_hip_sink_depth",
        "pose_wr_pre_snap_stance",
        "pose_wr_release_technique",
        "pose_qb_stride_consistency",
        "pose_qb_shoulder_hip_separation",
        "pose_qb_weight_transfer",
        "pose_qb_release_consistency",
        "pose_stride_symmetry",
        "pose_biomechanical_drift",
        "pose_body_orientation_proxy",
    }
)

# Position-group → relevant metric names for filtering
_POSITION_GROUP_METRICS: dict[str, frozenset[str]] = {
    "OL": frozenset(
        {
            "pose_pad_level",
            "pose_block_shed_timing",
            "pose_pass_set_weight_distribution",
            "pose_stride_symmetry",
            "pose_biomechanical_drift",
            "pose_body_orientation_proxy",
        }
    ),
    "DL": frozenset(
        {
            "pose_pad_level",
            "pose_block_shed_timing",
            "pose_stride_symmetry",
            "pose_biomechanical_drift",
            "pose_body_orientation_proxy",
        }
    ),
    "WR": frozenset(
        {
            "pose_wr_shoulder_over_knee",
            "pose_wr_deceleration_steps",
            "pose_wr_hip_sink_depth",
            "pose_wr_pre_snap_stance",
            "pose_wr_release_technique",
            "pose_stride_symmetry",
            "pose_body_orientation_proxy",
        }
    ),
    "QB": frozenset(
        {
            "pose_qb_stride_consistency",
            "pose_qb_shoulder_hip_separation",
            "pose_qb_weight_transfer",
            "pose_qb_release_consistency",
            "pose_stride_symmetry",
            "pose_body_orientation_proxy",
        }
    ),
    "DB": frozenset({"pose_body_orientation_proxy", "pose_stride_symmetry"}),
    "LB": frozenset({"pose_body_orientation_proxy", "pose_stride_symmetry"}),
}


# ── Schemas ───────────────────────────────────────────────────────────────────


class PoseKeypointsCreate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    tracklet_id: uuid.UUID = Field(...)
    frame_number: int = Field(..., ge=0)
    keypoints: list[dict[str, Any]]
    head_yaw_degrees: float | None = None
    head_orientation_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    biomechanics: dict[str, Any] | None = None
    model_version_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


class PoseKeypointsResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: uuid.UUID
    tracklet_id: uuid.UUID | None
    frame_number: int
    keypoints: list[dict[str, Any]]
    head_yaw_degrees: float | None
    head_orientation_confidence: float | None
    biomechanics: dict[str, Any] | None
    model_version_id: uuid.UUID | None
    job_id: uuid.UUID | None
    created_at: str

    @classmethod
    def from_orm(cls, pk: PoseKeypoints) -> "PoseKeypointsResponse":
        return cls(
            id=pk.id,
            tracklet_id=pk.tracklet_id,
            frame_number=pk.frame_number,
            keypoints=pk.keypoints,
            head_yaw_degrees=pk.head_yaw_degrees,
            head_orientation_confidence=pk.head_orientation_confidence,
            biomechanics=pk.biomechanics,
            model_version_id=pk.model_version_id,
            job_id=pk.job_id,
            created_at=pk.created_at.isoformat(),
        )


class PoseMetricSummary(BaseModel):
    """Lightweight metric summary for the pending-review list."""

    id: uuid.UUID
    clip_id: uuid.UUID
    tracklet_id: uuid.UUID | None
    metric_name: str
    metric_value: dict[str, Any]
    confidence: float | None
    experimental_flag: bool
    analytics_safe: bool
    created_at: str

    @classmethod
    def from_orm(cls, m: Metric) -> "PoseMetricSummary":
        return cls(
            id=m.id,
            clip_id=m.clip_id,
            tracklet_id=m.tracklet_id,
            metric_name=m.metric_name,
            metric_value=m.metric_value,
            confidence=m.confidence,
            experimental_flag=m.experimental_flag,
            analytics_safe=m.analytics_safe,
            created_at=m.created_at.isoformat(),
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
    def from_orm(cls, r: HeadOrientationReview) -> "ReviewResponse":
        return cls(
            id=r.id,
            metric_id=r.metric_id,
            reviewer_id=r.reviewer_id,
            review_action=r.review_action,
            position_group=r.position_group,
            notes=r.notes,
            created_at=r.created_at.isoformat(),
        )


class AsymmetryEntry(BaseModel):
    metric_id: uuid.UUID
    clip_id: uuid.UUID
    tracklet_id: uuid.UUID | None
    left_right_ratio: float
    asymmetry_score: float
    injury_risk_flag: bool
    confidence: float | None
    created_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/keypoints",
    response_model=PoseKeypointsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pose_keypoints(
    body: PoseKeypointsCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
) -> PoseKeypointsResponse:
    """Store raw pose keypoints for a tracklet frame.

    Called by the GPU worker pose stage after running RTMPose or the stub
    estimator.  The keypoints are stored in the ``pose_keypoints`` table with
    an optional ``biomechanics`` JSONB column for pre-computed per-frame angles.
    """
    t_result = await db.execute(select(Tracklet).where(Tracklet.id == body.tracklet_id))
    if t_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracklet not found")

    pk = PoseKeypoints(
        id=uuid.uuid4(),
        tracklet_id=body.tracklet_id,
        frame_number=body.frame_number,
        keypoints=body.keypoints,
        head_yaw_degrees=body.head_yaw_degrees,
        head_orientation_confidence=body.head_orientation_confidence,
        biomechanics=body.biomechanics,
        model_version_id=body.model_version_id,
        job_id=body.job_id,
    )
    db.add(pk)
    await db.flush()
    log.info(
        "pose_keypoints_created",
        id=str(pk.id),
        tracklet_id=str(body.tracklet_id),
        frame_number=body.frame_number,
    )
    return PoseKeypointsResponse.from_orm(pk)


@router.get("/keypoints", response_model=list[PoseKeypointsResponse])
async def list_pose_keypoints(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    tracklet_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[PoseKeypointsResponse]:
    """List raw pose keypoints, optionally filtered by tracklet.

    Player- and viewer-role users are blocked: raw keypoints are an internal
    coaching artifact and must never be surfaced in player- or fan-facing
    views, regardless of any per-metric ``analytics_safe`` flag.
    """
    if current_user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pose keypoints are not available in player-facing views",
        )

    q = select(PoseKeypoints)
    if tracklet_id is not None:
        q = q.where(PoseKeypoints.tracklet_id == tracklet_id)
    q = q.order_by(PoseKeypoints.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    return [PoseKeypointsResponse.from_orm(pk) for pk in result.scalars().all()]


@router.get("/keypoints/{keypoint_id}", response_model=PoseKeypointsResponse)
async def get_pose_keypoints(
    keypoint_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PoseKeypointsResponse:
    """Get a single pose-keypoints row by ID.

    Player- and viewer-role users are blocked (see ``list_pose_keypoints``).
    """
    if current_user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Pose keypoints are not available in player-facing views",
        )
    result = await db.execute(select(PoseKeypoints).where(PoseKeypoints.id == keypoint_id))
    pk = result.scalar_one_or_none()
    if pk is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pose keypoints not found"
        )
    return PoseKeypointsResponse.from_orm(pk)


@router.get("/metrics/pending", response_model=list[PoseMetricSummary])
async def list_pending_pose_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
    position_group: str | None = Query(
        default=None,
        description="Filter by position group: OL, DL, WR, QB.  Omit for all groups.",
    ),
    clip_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[PoseMetricSummary]:
    """List pose biomechanics metrics that are awaiting position-coach review.

    Returns only metrics where:
      - ``experimental_flag = True``
      - ``analytics_safe = False``
      - ``metric_name`` starts with ``pose_``

    Optionally filters to metrics relevant to a specific position group.
    """
    relevant_names = _position_group_metric_names(position_group)

    q = (
        select(Metric)
        .where(Metric.experimental_flag.is_(True))
        .where(Metric.analytics_safe.is_(False))
        .where(Metric.metric_name.in_(relevant_names))
        .order_by(Metric.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if clip_id is not None:
        q = q.where(Metric.clip_id == clip_id)

    result = await db.execute(q)
    return [PoseMetricSummary.from_orm(m) for m in result.scalars().all()]


@router.post(
    "/metrics/{metric_id}/review",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def review_pose_metric(
    metric_id: uuid.UUID,
    body: ReviewCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ReviewResponse:
    """Position-coach review of a pose biomechanics metric.

    - **approve**: sets ``analytics_safe=True`` — metric visible in staff dashboards
    - **reject**: metric stays suppressed; model team notified via log
    - **flag**: routed to model review queue

    Only experimental pose metrics (``metric_name`` starts with ``pose_``) are
    accepted.  Player-facing views never see pose metrics regardless of this.
    """
    result = await db.execute(select(Metric).where(Metric.id == metric_id))
    metric = result.scalar_one_or_none()
    if metric is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found")

    if not metric.metric_name.startswith("pose_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint only reviews pose biomechanics metrics (pose_* names)",
        )
    if not metric.experimental_flag:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only experimental metrics require position-coach review",
        )

    review = HeadOrientationReview(
        id=uuid.uuid4(),
        metric_id=metric.id,
        reviewer_id=current_user.id,
        review_action=body.review_action,
        position_group=body.position_group,
        notes=body.notes,
        created_at=datetime.now(UTC),
    )
    db.add(review)

    if body.review_action == "approve":
        metric.analytics_safe = True
        log.info(
            "pose_metric_approved",
            metric_id=str(metric_id),
            metric_name=metric.metric_name,
            reviewer_id=str(current_user.id),
            position_group=body.position_group,
        )
    elif body.review_action == "reject":
        metric.analytics_safe = False
        log.info(
            "pose_metric_rejected",
            metric_id=str(metric_id),
            metric_name=metric.metric_name,
            reviewer_id=str(current_user.id),
        )
    else:
        log.info(
            "pose_metric_flagged_for_model_review",
            metric_id=str(metric_id),
            metric_name=metric.metric_name,
            reviewer_id=str(current_user.id),
        )

    await db.flush()
    return ReviewResponse.from_orm(review)


@router.get("/asymmetry", response_model=list[AsymmetryEntry])
async def get_asymmetry_trends(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_sportsperformance_or_above)],
    player_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[AsymmetryEntry]:
    """Stride asymmetry trends for sports-performance risk monitoring.

    Returns ``pose_stride_symmetry`` metrics, optionally filtered to a player's
    tracklets.  Access is restricted to the ``sportsperformance`` role because
    asymmetry flags feed the UT Athletics injury-prediction model.

    Injury risk flags (``injury_risk_flag=True``) are never forwarded to coaches
    or players from this endpoint — only sports performance staff.
    """
    q = (
        select(Metric)
        .where(Metric.metric_name == "pose_stride_symmetry")
        .order_by(Metric.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    if player_id is not None:
        # Join via tracklets to filter by player
        q = q.join(Tracklet, Metric.tracklet_id == Tracklet.id).where(
            Tracklet.player_id == player_id
        )

    result = await db.execute(q)
    metrics = result.scalars().all()

    entries: list[AsymmetryEntry] = []
    for m in metrics:
        val = m.metric_value
        entries.append(
            AsymmetryEntry(
                metric_id=m.id,
                clip_id=m.clip_id,
                tracklet_id=m.tracklet_id,
                left_right_ratio=float(val.get("left_right_ratio", 1.0)),
                asymmetry_score=float(val.get("asymmetry_score", 0.0)),
                injury_risk_flag=bool(val.get("injury_risk_flag", False)),
                confidence=m.confidence,
                created_at=m.created_at.isoformat(),
            )
        )
    return entries


# ── Helpers ───────────────────────────────────────────────────────────────────


def _position_group_metric_names(position_group: str | None) -> frozenset[str]:
    """Return the metric names relevant to the given position group."""
    if position_group is None:
        return POSE_METRIC_NAMES
    pg = position_group.upper()
    return _POSITION_GROUP_METRICS.get(pg, POSE_METRIC_NAMES)
