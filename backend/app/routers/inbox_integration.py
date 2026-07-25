"""Practice Inbox integration router (Issue #16).

Surfaces real-time video processing status in the Practice Inbox (Issue #4).

Endpoints:
  GET /api/v1/inbox/status          — processing status feed for all active jobs
  GET /api/v1/inbox/status/{job_id} — status for a single job

The Practice Inbox dashboard must show:
  - Uploaded videos and their processing status
  - Failed stage and retry option
  - Number of detected clips per video
  - Calibration-safe percentage
  - Same-session period-break jobs with estimated delivery time
  - Whether pose pipeline (Issue #6) is active for the session

Access: any staff role (admin, analyst, coach, sportsperformance).
Players and viewers are blocked.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff
from app.models import (
    Clip,
    ClipResultState,
    FieldCalibration,
    JobStatus,
    JobType,
    ProcessingJob,
    User,
    Video,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])

# Priority threshold that marks a job as same-session (from Copilot queue module)
_SAME_SESSION_PRIORITY = 10


# ── Schemas ───────────────────────────────────────────────────────────────────


class VideoInboxItem(BaseModel):
    """Status entry for one video in the Practice Inbox."""

    video_id: uuid.UUID
    filename: str
    video_status: str
    total_jobs: int
    running_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    clip_count: int
    # Clips whose results are still the same-session first pass (Issue #147) —
    # awaiting the nightly full-quality upgrade. Surfaced as a "Preliminary"
    # badge in the Practice Inbox.
    preliminary_clip_count: int
    calibration_safe_pct: float | None
    latest_error_stage: str | None
    latest_error_message: str | None
    same_session_job_count: int
    pose_pipeline_active: bool
    created_at: str


class JobStatusItem(BaseModel):
    """Lightweight job status entry."""

    job_id: uuid.UUID
    video_id: uuid.UUID | None
    clip_id: uuid.UUID | None
    job_type: str
    status: str
    priority: int
    pipeline_mode: str | None
    is_same_session: bool
    nightly_followup_job_id: uuid.UUID | None
    started_at: str | None
    finished_at: str | None
    error_stage: str | None
    error_message: str | None
    created_at: str

    @classmethod
    def from_orm(cls, j: ProcessingJob) -> JobStatusItem:
        return cls(
            job_id=j.id,
            video_id=j.video_id,
            clip_id=j.clip_id,
            job_type=j.job_type.value,
            status=j.status.value,
            priority=j.priority,
            pipeline_mode=j.pipeline_mode,
            is_same_session=j.priority >= _SAME_SESSION_PRIORITY,
            nightly_followup_job_id=j.nightly_followup_job_id,
            started_at=j.started_at.isoformat() if j.started_at else None,
            finished_at=j.finished_at.isoformat() if j.finished_at else None,
            error_stage=j.error_stage,
            error_message=j.error_message,
            created_at=j.created_at.isoformat(),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/status", response_model=list[VideoInboxItem])
async def get_inbox_status(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_staff)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_succeeded: bool = Query(
        default=False,
        description="Include videos where all jobs succeeded (default: active only)",
    ),
) -> list[VideoInboxItem]:
    """Return processing status for recent videos in the Practice Inbox.

    Returns the most recent videos with their job counts, clip counts,
    calibration-safe percentage, same-session job counts, and any errors.
    """
    # Fetch recent videos
    video_q = select(Video).order_by(Video.created_at.desc()).limit(limit).offset(offset)
    if not include_succeeded:
        video_q = video_q.where(Video.status.notin_(["ready"]))

    video_result = await db.execute(video_q)
    videos = video_result.scalars().all()

    items: list[VideoInboxItem] = []
    for video in videos:
        item = await _build_video_inbox_item(db, video)
        items.append(item)

    return items


@router.get("/status/{job_id}", response_model=JobStatusItem)
async def get_job_status(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> JobStatusItem:
    """Return the status of a single processing job."""
    result = await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobStatusItem.from_orm(job)


@router.get("/jobs", response_model=list[JobStatusItem])
async def list_active_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
    video_id: uuid.UUID | None = Query(default=None),
    same_session_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobStatusItem]:
    """List active processing jobs, optionally filtered to same-session jobs."""
    q = (
        select(ProcessingJob)
        .where(ProcessingJob.status.in_([JobStatus.queued, JobStatus.running]))
        .order_by(ProcessingJob.priority.desc(), ProcessingJob.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    if video_id is not None:
        q = q.where(ProcessingJob.video_id == video_id)
    if same_session_only:
        q = q.where(ProcessingJob.priority >= _SAME_SESSION_PRIORITY)

    result = await db.execute(q)
    return [JobStatusItem.from_orm(j) for j in result.scalars().all()]


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _build_video_inbox_item(db: AsyncSession, video: Video) -> VideoInboxItem:
    """Build a VideoInboxItem by aggregating job and clip data for one video."""
    jobs_result = await db.execute(select(ProcessingJob).where(ProcessingJob.video_id == video.id))
    jobs = jobs_result.scalars().all()

    total_jobs = len(jobs)
    running_jobs = sum(1 for j in jobs if j.status == JobStatus.running)
    succeeded_jobs = sum(1 for j in jobs if j.status == JobStatus.succeeded)
    failed_jobs = sum(1 for j in jobs if j.status == JobStatus.failed)
    same_session_jobs = sum(1 for j in jobs if j.priority >= _SAME_SESSION_PRIORITY)

    # Latest error
    failed = [j for j in jobs if j.status == JobStatus.failed]
    latest_failed = max(failed, key=lambda j: j.created_at) if failed else None

    # Clip count
    clip_result = await db.execute(
        select(func.count()).select_from(Clip).where(Clip.video_id == video.id)
    )
    clip_count = clip_result.scalar_one() or 0

    # Preliminary clips awaiting nightly upgrade (Issue #147)
    prelim_result = await db.execute(
        select(func.count())
        .select_from(Clip)
        .where(
            Clip.video_id == video.id,
            Clip.result_state == ClipResultState.preliminary.value,
        )
    )
    preliminary_clip_count = prelim_result.scalar_one() or 0

    # Calibration safe %
    cal_result = await db.execute(
        select(FieldCalibration).where(FieldCalibration.video_id == video.id)
    )
    calibrations = cal_result.scalars().all()
    cal_safe_pct: float | None = None
    if calibrations:
        safe = sum(1 for c in calibrations if c.analytics_safe)
        cal_safe_pct = round(safe / len(calibrations) * 100, 1)

    # Pose pipeline active: check if any pose job succeeded
    pose_active = any(j.job_type == JobType.pose and j.status == JobStatus.succeeded for j in jobs)

    return VideoInboxItem(
        video_id=video.id,
        filename=video.filename,
        video_status=video.status.value,
        total_jobs=total_jobs,
        running_jobs=running_jobs,
        succeeded_jobs=succeeded_jobs,
        failed_jobs=failed_jobs,
        clip_count=clip_count,
        preliminary_clip_count=preliminary_clip_count,
        calibration_safe_pct=cal_safe_pct,
        latest_error_stage=latest_failed.error_stage if latest_failed else None,
        latest_error_message=latest_failed.error_message if latest_failed else None,
        same_session_job_count=same_session_jobs,
        pose_pipeline_active=pose_active,
        created_at=video.created_at.isoformat(),
    )
