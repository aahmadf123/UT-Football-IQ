"""Videos router — upload records, status, and presigned URL helpers."""

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import (
    CaptureRegime,
    JobStatus,
    JobType,
    PipelineMode,
    ProcessingJob,
    SessionKind,
    SideOfBall,
    SourceType,
    SystemSetting,
    User,
    Video,
    VideoStatus,
)
from app.schemas.settings import SYSTEM_CONFIG_KEY, SystemConfig
from app.workload import WorkloadStatus, assess_workload

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/videos", tags=["videos"])


async def _auto_process_enabled(db: AsyncSession) -> bool:
    """Read the set-and-forget toggle (defaults ON when no row exists)."""
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == SYSTEM_CONFIG_KEY))
    ).scalar_one_or_none()
    try:
        config = SystemConfig(**(row.value if row else {}))
    except Exception:
        config = SystemConfig()
    return config.auto_process_on_upload


async def _maybe_auto_enqueue_pipeline(db: AsyncSession, video: Video) -> ProcessingJob | None:
    """Create the video's pipeline job when auto-process is on.

    Best-effort: saturation or any failure leaves the video ``uploaded`` (the
    coach can still press Process Film) rather than failing the registration.
    """
    if not await _auto_process_enabled(db):
        return None
    snapshot = await assess_workload(db)
    if snapshot.status == WorkloadStatus.saturated:
        log.warning(
            "auto_process_deferred_saturated",
            video_id=str(video.id),
            queued=snapshot.queued,
            running=snapshot.running,
        )
        return None
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.queued,
        priority=0,
        pipeline_mode=PipelineMode.nightly,
        input_artifacts={"auto_enqueued": True},
    )
    db.add(job)
    await db.flush()
    log.info("auto_process_enqueued", video_id=str(video.id), job_id=str(job.id))
    return job


# ── Schemas ───────────────────────────────────────────────────────────────────


class VideoCreate(BaseModel):
    filename: str
    storage_uri: str
    recorded_at: datetime | None = None
    session_kind: SessionKind | None = None
    source_type: SourceType | None = None
    opponent_team: str | None = None
    practice_session_id: uuid.UUID | None = None
    our_possession: SideOfBall | None = None

    @model_validator(mode="after")
    def _opponent_required_for_game(self) -> "VideoCreate":
        if self.session_kind == SessionKind.game and not (
            self.opponent_team and self.opponent_team.strip()
        ):
            raise ValueError("opponent_team is required when session_kind='game'")
        return self


class VideoStatusUpdate(BaseModel):
    status: VideoStatus
    duration_seconds: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    capture_regime: CaptureRegime | None = None
    regime_confidence: float | None = None


class VideoResponse(BaseModel):
    id: uuid.UUID
    filename: str
    storage_uri: str
    status: VideoStatus
    duration_seconds: float | None
    fps: float | None
    width: int | None
    height: int | None
    codec: str | None
    uploaded_by: uuid.UUID | None
    recorded_at: datetime | None
    session_kind: SessionKind | None
    source_type: SourceType | None
    opponent_team: str | None
    practice_session_id: uuid.UUID | None
    our_possession: SideOfBall | None
    capture_regime: CaptureRegime | None
    regime_confidence: float | None
    metadata_: dict[str, Any] | None = None
    created_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_video(cls, v: Video) -> "VideoResponse":
        return cls(
            id=v.id,
            filename=v.filename,
            storage_uri=v.storage_uri,
            status=v.status,
            duration_seconds=v.duration_seconds,
            fps=v.fps,
            width=v.width,
            height=v.height,
            codec=v.codec,
            uploaded_by=v.uploaded_by,
            recorded_at=v.recorded_at,
            session_kind=v.session_kind,
            source_type=v.source_type,
            opponent_team=v.opponent_team,
            practice_session_id=v.practice_session_id,
            our_possession=v.our_possession,
            capture_regime=v.capture_regime,
            regime_confidence=v.regime_confidence,
            metadata_=v.metadata_,
            created_at=v.created_at.isoformat(),
        )


class JobSummary(BaseModel):
    id: uuid.UUID
    job_type: str
    status: str
    error_stage: str | None
    error_message: str | None
    created_at: str

    model_config = {"from_attributes": True}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[VideoResponse])
async def list_videos(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: VideoStatus | None = Query(default=None, alias="status"),
    recorded_after: datetime | None = Query(default=None),
    recorded_before: datetime | None = Query(default=None),
    session_kind: SessionKind | None = Query(default=None),
    source_type: SourceType | None = Query(default=None),
    opponent_team: str | None = Query(default=None),
    practice_session_id: uuid.UUID | None = Query(default=None),
) -> list[VideoResponse]:
    """List videos with Hudl-style filters; ``recorded_at`` newest first."""
    q = (
        select(Video)
        .order_by(Video.recorded_at.desc().nullslast(), Video.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status_filter is not None:
        q = q.where(Video.status == status_filter)
    if recorded_after is not None:
        q = q.where(Video.recorded_at >= recorded_after)
    if recorded_before is not None:
        q = q.where(Video.recorded_at <= recorded_before)
    if session_kind is not None:
        q = q.where(Video.session_kind == session_kind)
    if source_type is not None:
        q = q.where(Video.source_type == source_type)
    if opponent_team is not None:
        q = q.where(Video.opponent_team == opponent_team)
    if practice_session_id is not None:
        q = q.where(Video.practice_session_id == practice_session_id)
    result = await db.execute(q)
    videos = result.scalars().all()
    return [VideoResponse.from_orm_video(v) for v in videos]


@router.post("", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
async def create_video(
    body: VideoCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_staff)],
) -> VideoResponse:
    """Register a new video record after the file has been uploaded to R2."""
    video = Video(
        id=uuid.uuid4(),
        filename=body.filename,
        storage_uri=body.storage_uri,
        status=VideoStatus.uploaded,
        uploaded_by=current_user.id,
        recorded_at=body.recorded_at,
        session_kind=body.session_kind,
        source_type=body.source_type,
        opponent_team=body.opponent_team,
        practice_session_id=body.practice_session_id,
        our_possession=body.our_possession,
    )
    db.add(video)
    await db.flush()
    log.info(
        "video_created",
        video_id=str(video.id),
        filename=video.filename,
        session_kind=str(video.session_kind) if video.session_kind else None,
        source_type=str(video.source_type) if video.source_type else None,
    )
    try:
        await _maybe_auto_enqueue_pipeline(db, video)
    except Exception as exc:
        log.warning("auto_process_enqueue_failed", video_id=str(video.id), error=str(exc))
    return VideoResponse.from_orm_video(video)


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> VideoResponse:
    """Get a single video record."""
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    return VideoResponse.from_orm_video(video)


@router.patch("/{video_id}/status", response_model=VideoResponse)
async def update_video_status(
    video_id: uuid.UUID,
    body: VideoStatusUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> VideoResponse:
    """Update video processing status and optional metadata."""
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    video.status = body.status
    if body.duration_seconds is not None:
        video.duration_seconds = body.duration_seconds
    if body.fps is not None:
        video.fps = body.fps
    if body.width is not None:
        video.width = body.width
    if body.height is not None:
        video.height = body.height
    if body.codec is not None:
        video.codec = body.codec
    if body.capture_regime is not None:
        video.capture_regime = body.capture_regime
    if body.regime_confidence is not None:
        video.regime_confidence = body.regime_confidence

    await db.flush()
    log.info(
        "video_status_updated",
        video_id=str(video_id),
        status=body.status,
        capture_regime=str(video.capture_regime) if video.capture_regime else None,
    )
    return VideoResponse.from_orm_video(video)


@router.get("/{video_id}/jobs", response_model=list[JobSummary])
async def list_video_jobs(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> list[JobSummary]:
    """List processing jobs for a video, newest first."""
    result = await db.execute(select(Video).where(Video.id == video_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    jobs_result = await db.execute(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video_id)
        .order_by(ProcessingJob.created_at.desc())
    )
    jobs = jobs_result.scalars().all()
    return [
        JobSummary(
            id=j.id,
            job_type=j.job_type.value,
            status=j.status.value,
            error_stage=j.error_stage,
            error_message=j.error_message,
            created_at=j.created_at.isoformat(),
        )
        for j in jobs
    ]
