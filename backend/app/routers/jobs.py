"""Jobs router — processing job observability, retry, and worker claiming.

The ``processing_jobs`` table IS the queue: the GPU worker claims queued
rows via ``POST /claim`` (single ``UPDATE … WHERE id = (SELECT … FOR UPDATE
SKIP LOCKED)``) and keeps its lease alive via ``POST /{id}/heartbeat``.
Expired leases are lazily reclaimed by the next claim call; rows whose
``attempt_count`` reaches ``max_attempts`` are swept to ``failed``.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import JobStatus, JobType, PipelineMode, ProcessingJob, User, UserRole, Video
from app.workload import WorkloadSnapshot, require_workload_capacity

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class JobCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID | None = None
    video_id: uuid.UUID
    job_type: JobType
    priority: int = 0
    pipeline_mode: str | None = None
    input_artifacts: dict[str, Any] | None = None
    model_version_id: uuid.UUID | None = None


class JobStatusUpdate(BaseModel):
    status: JobStatus
    error_stage: str | None = None
    error_message: str | None = None
    output_artifacts: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    # The lease holder's id. Required to transition a leased running job
    # unless the caller is an admin (ops override) — see update_job_status.
    worker_id: str | None = Field(default=None, max_length=128)


#: Ceiling for caller-supplied JSON blobs merged into a job row. Progress
#: and artifacts are summaries; anything larger is a mistake or a per-
#: heartbeat row-bloat attack.
_JSON_BLOB_LIMIT_BYTES = 64 * 1024


def _reject_json_bomb(field: str, value: dict[str, Any]) -> None:
    size = len(json.dumps(value, default=str).encode())
    if size > _JSON_BLOB_LIMIT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{field} payload exceeds {_JSON_BLOB_LIMIT_BYTES} bytes",
        )


class JobClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    job_types: list[JobType] | None = None
    lease_seconds: int = Field(default=600, ge=30, le=3600)


class JobHeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int = Field(default=600, ge=30, le=3600)
    progress: dict[str, Any] | None = None


class JobResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    video_id: uuid.UUID | None
    clip_id: uuid.UUID | None
    job_type: str
    status: str
    priority: int
    pipeline_mode: str | None
    is_same_session: bool
    error_stage: str | None
    error_message: str | None
    nightly_followup_job_id: uuid.UUID | None
    input_artifacts: dict[str, Any] | None
    output_artifacts: dict[str, Any] | None
    model_version_id: uuid.UUID | None
    progress: dict[str, Any] | None
    attempt_count: int
    leased_by: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str

    @classmethod
    def from_orm_job(cls, j: ProcessingJob) -> "JobResponse":
        return cls(
            id=j.id,
            video_id=j.video_id,
            clip_id=j.clip_id,
            job_type=j.job_type.value,
            status=j.status.value,
            priority=j.priority,
            pipeline_mode=j.pipeline_mode,
            is_same_session=j.priority >= 10,
            error_stage=j.error_stage,
            error_message=j.error_message,
            nightly_followup_job_id=j.nightly_followup_job_id,
            input_artifacts=j.input_artifacts,
            output_artifacts=j.output_artifacts,
            model_version_id=j.model_version_id,
            progress=j.progress,
            # Column defaults apply at flush; a not-yet-flushed row reads None.
            attempt_count=j.attempt_count or 0,
            leased_by=j.leased_by,
            started_at=j.started_at.isoformat() if j.started_at else None,
            finished_at=j.finished_at.isoformat() if j.finished_at else None,
            created_at=j.created_at.isoformat(),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    job_type: JobType | None = Query(default=None),
    video_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[JobResponse]:
    """List processing jobs with optional filters, newest first."""
    q = select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(limit).offset(offset)
    if status_filter is not None:
        q = q.where(ProcessingJob.status == status_filter)
    if job_type is not None:
        q = q.where(ProcessingJob.job_type == job_type)
    if video_id is not None:
        q = q.where(ProcessingJob.video_id == video_id)
    result = await db.execute(q)
    return [JobResponse.from_orm_job(j) for j in result.scalars().all()]


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
    _workload: Annotated[WorkloadSnapshot, Depends(require_workload_capacity("jobs.create"))],
) -> JobResponse:
    """Submit a new processing job for a video.

    Gated by :func:`app.workload.require_workload_capacity` — when the GPU
    queue is saturated this returns 503 ``workload_gated`` instead of
    accepting the job.
    """
    vid_result = await db.execute(select(Video).where(Video.id == body.video_id))
    if vid_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    mode = body.pipeline_mode or (
        PipelineMode.same_session if body.priority >= 10 else PipelineMode.nightly
    )
    job = ProcessingJob(
        id=body.id or uuid.uuid4(),
        video_id=body.video_id,
        job_type=body.job_type,
        status=JobStatus.queued,
        priority=body.priority,
        pipeline_mode=mode,
        input_artifacts=body.input_artifacts,
        model_version_id=body.model_version_id,
    )
    db.add(job)
    await db.flush()
    log.info("job_created", job_id=str(job.id), job_type=body.job_type, video_id=str(body.video_id))
    return JobResponse.from_orm_job(job)


@router.post("/claim", response_model=JobResponse | None)
async def claim_job(
    body: JobClaimRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> JobResponse | None:
    """Atomically claim the next runnable job for a worker.

    Claimable rows are ``queued`` rows and ``running`` rows whose lease has
    expired (crashed/stalled worker), ordered by priority then age. Claiming
    is a single ``UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`` so
    concurrent workers never double-claim. Returns 204 when nothing is
    runnable.
    """
    now = datetime.now(UTC)

    # Sweep: expired-lease rows that exhausted their attempts fail terminally.
    await db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.status == JobStatus.running,
            ProcessingJob.lease_expires_at.is_not(None),
            ProcessingJob.lease_expires_at < now,
            ProcessingJob.attempt_count >= ProcessingJob.max_attempts,
        )
        .values(
            status=JobStatus.failed,
            error_message="lease expired; attempts exhausted",
            finished_at=now,
            leased_by=None,
            lease_expires_at=None,
        )
    )

    claimable = or_(
        ProcessingJob.status == JobStatus.queued,
        and_(
            ProcessingJob.status == JobStatus.running,
            ProcessingJob.lease_expires_at.is_not(None),
            ProcessingJob.lease_expires_at < now,
        ),
    )
    candidate = (
        select(ProcessingJob.id)
        .where(claimable, ProcessingJob.attempt_count < ProcessingJob.max_attempts)
        .order_by(ProcessingJob.priority.desc(), ProcessingJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if body.job_types:
        candidate = candidate.where(ProcessingJob.job_type.in_(body.job_types))

    result = await db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == candidate.scalar_subquery())
        .values(
            status=JobStatus.running,
            leased_by=body.worker_id,
            lease_expires_at=now + timedelta(seconds=body.lease_seconds),
            attempt_count=ProcessingJob.attempt_count + 1,
            started_at=func.coalesce(ProcessingJob.started_at, now),
        )
        .returning(ProcessingJob)
    )
    job = result.scalar_one_or_none()
    if job is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    await db.flush()
    log.info(
        "job_claimed",
        job_id=str(job.id),
        worker_id=body.worker_id,
        job_type=job.job_type.value,
        attempt=job.attempt_count,
    )
    return JobResponse.from_orm_job(job)


@router.post("/{job_id}/heartbeat", response_model=JobResponse)
async def heartbeat_job(
    job_id: uuid.UUID,
    body: JobHeartbeatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> JobResponse:
    """Extend a claimed job's lease and merge per-stage progress."""
    result = await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status != JobStatus.running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is not running (current status: {job.status})",
        )
    if job.leased_by is not None and job.leased_by != body.worker_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is leased by another worker ({job.leased_by})",
        )

    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=body.lease_seconds)
    if body.progress:
        _reject_json_bomb("progress", body.progress)
        merged = dict(job.progress or {})
        merged.update(body.progress)
        _reject_json_bomb("progress", merged)
        job.progress = merged
    await db.flush()
    return JobResponse.from_orm_job(job)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Get full job details including error reason, stage, and input file."""
    result = await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobResponse.from_orm_job(job)


@router.patch("/{job_id}", response_model=JobResponse)
async def update_job_status(
    job_id: uuid.UUID,
    body: JobStatusUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_any_staff)],
) -> JobResponse:
    """Update job status — used by GPU worker callbacks."""
    result = await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # A leased running job belongs to its worker: state changes must carry
    # the lease holder's worker_id. Admins may override (ops cancel).
    if (
        job.status == JobStatus.running
        and job.leased_by is not None
        and body.worker_id != job.leased_by
        and current_user.role != UserRole.admin
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is leased by another worker ({job.leased_by})",
        )
    if body.output_artifacts is not None:
        _reject_json_bomb("output_artifacts", body.output_artifacts)
    if body.progress is not None:
        _reject_json_bomb("progress", body.progress)

    job.status = body.status
    if body.status == JobStatus.running and job.started_at is None:
        job.started_at = datetime.now(UTC)
    if body.status in (JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled):
        job.finished_at = datetime.now(UTC)
        # Terminal states release the worker lease.
        job.leased_by = None
        job.lease_expires_at = None
    if body.error_stage is not None:
        job.error_stage = body.error_stage
    if body.error_message is not None:
        job.error_message = body.error_message
    if body.output_artifacts is not None:
        job.output_artifacts = body.output_artifacts
    if body.progress is not None:
        merged = dict(job.progress or {})
        merged.update(body.progress)
        job.progress = merged

    await db.flush()
    log.info("job_status_updated", job_id=str(job_id), status=body.status)
    return JobResponse.from_orm_job(job)


@router.post("/{job_id}/retry", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def retry_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
    _workload: Annotated[WorkloadSnapshot, Depends(require_workload_capacity("jobs.retry"))],
) -> JobResponse:
    """Retry a failed job by creating a new queued copy.

    Subject to the same workload gating as :func:`create_job` — retrying a
    job under saturation just defers the problem.
    """
    result = await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    original = result.scalar_one_or_none()
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if original.status != JobStatus.failed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed jobs can be retried (current status: {original.status})",
        )

    retry = ProcessingJob(
        id=uuid.uuid4(),
        video_id=original.video_id,
        clip_id=original.clip_id,
        job_type=original.job_type,
        status=JobStatus.queued,
        priority=original.priority,
        pipeline_mode=original.pipeline_mode,
        input_artifacts=original.input_artifacts,
        model_version_id=original.model_version_id,
    )
    db.add(retry)
    await db.flush()
    log.info("job_retried", original_job_id=str(job_id), new_job_id=str(retry.id))
    return JobResponse.from_orm_job(retry)
