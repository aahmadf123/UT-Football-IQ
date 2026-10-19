"""Jobs router — processing job observability, retry, and worker claiming.

The ``processing_jobs`` table IS the queue: the GPU worker claims queued
rows via ``POST /claim`` (single ``UPDATE … WHERE id = (SELECT … FOR UPDATE
SKIP LOCKED)``) and keeps its lease alive via ``POST /{id}/heartbeat``.
Expired leases are reclaimed by :func:`app.services.job_sweeper.sweep_expired_leases`,
which runs here before every claim *and* on the nightly tick — the claim-time
sweep alone leaves stalled rows sitting in ``running`` whenever no worker is
polling.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import (
    JobStatus,
    JobType,
    PipelineMode,
    ProcessingJob,
    User,
    UserRole,
    Video,
    WorkerRuntime,
)
from app.services.job_sweeper import sweep_expired_leases
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
    processing_profile: Literal["review", "quick", "detailed"] | None = None


class JobStatusUpdate(BaseModel):
    status: JobStatus
    error_stage: str | None = None
    error_message: str | None = None
    output_artifacts: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    # The lease holder's id. Required to transition a leased running job
    # unless the caller is an admin (ops override) — see update_job_status.
    worker_id: str | None = Field(default=None, max_length=128)
    lease_token: uuid.UUID | None = None
    attempt_count: int | None = None
    provenance: dict[str, Any] | None = None


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
    capabilities: dict[str, Any] | None = None
    lease_seconds: int = Field(default=600, ge=30, le=3600)


class JobHeartbeatRequest(BaseModel):
    lease_token: uuid.UUID | None = None
    attempt_count: int | None = None
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
    lease_token: uuid.UUID | None = None
    control_requested: str | None = None
    provenance: dict[str, Any] | None = None
    started_at: str | None
    finished_at: str | None
    created_at: str

    @classmethod
    def from_orm_job(cls, j: ProcessingJob) -> "JobResponse":
        return cls(
            provenance=j.provenance if isinstance(j.provenance, dict) else None,
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
            lease_token=j.lease_token if isinstance(j.lease_token, uuid.UUID) else None,
            control_requested=j.control_requested if isinstance(j.control_requested, str) else None,
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
    has_video: bool | None = Query(default=None),
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
    if has_video:
        q = q.where(ProcessingJob.video_id.is_not(None))
    result = await db.execute(q)
    return [JobResponse.from_orm_job(j) for j in result.scalars().all()]


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
    _workload: Annotated[WorkloadSnapshot, Depends(require_workload_capacity("jobs.create"))],
) -> JobResponse:
    """Submit a new processing job for a video.

    Gated by :func:`app.workload.require_workload_capacity` — when the GPU
    queue is saturated this returns 503 ``workload_gated`` instead of
    accepting the job.

    Idempotent for ``pipeline`` jobs: while a pipeline job for the video is
    still queued or running, a repeat POST returns that job (200) instead of
    stacking a duplicate full-pipeline run — "Process Film" clicked from a
    stale tab or a second device must not process the same video twice.
    """
    vid_result = await db.execute(select(Video).where(Video.id == body.video_id).with_for_update())
    if vid_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")

    if body.job_type == JobType.pipeline:
        existing = (
            await db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.video_id == body.video_id,
                    ProcessingJob.job_type == JobType.pipeline,
                    ProcessingJob.status.in_(
                        [JobStatus.queued, JobStatus.running, JobStatus.paused]
                    ),
                )
                .order_by(ProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.info(
                "job_create_deduplicated",
                video_id=str(body.video_id),
                existing_job_id=str(existing.id),
            )
            response.status_code = status.HTTP_200_OK
            return JobResponse.from_orm_job(existing)

    mode = body.pipeline_mode or (
        PipelineMode.same_session if body.priority >= 10 else PipelineMode.nightly
    )
    artifacts = dict(body.input_artifacts or {})
    if body.processing_profile:
        artifacts["processing_profile"] = body.processing_profile
    if body.id is not None:
        replay = await db.get(ProcessingJob, body.id)
        if replay is not None:
            if replay.video_id != body.video_id or replay.job_type != body.job_type:
                raise HTTPException(409, "Job operation ID already belongs to another request")
            response.status_code = 200
            return JobResponse.from_orm_job(replay)
    job = ProcessingJob(
        id=body.id or uuid.uuid4(),
        video_id=body.video_id,
        job_type=body.job_type,
        status=JobStatus.queued,
        priority=body.priority,
        pipeline_mode=mode,
        input_artifacts=artifacts or None,
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

    # Reclaim anything a dead worker left behind: exhausted attempts fail
    # terminally, the rest go back to ``queued``. The claim below would pick up
    # an expired-lease ``running`` row regardless, so this is about keeping the
    # queue's reported state honest as much as about retrying.
    await sweep_expired_leases(db, now)

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

    # Registered workers advertise compatible job types and profiles. Legacy
    # workers remain supported; native local workers always register first.
    runtime = (
        await db.execute(select(WorkerRuntime).where(WorkerRuntime.worker_id == body.worker_id))
    ).scalar_one_or_none()
    capabilities = body.capabilities or (runtime.capabilities if runtime else None)
    if capabilities:
        advertised_types = capabilities.get("job_types")
        if advertised_types:
            candidate = candidate.where(ProcessingJob.job_type.in_(advertised_types))
        profiles = capabilities.get("profiles")
        if profiles:
            requested_profile = ProcessingJob.input_artifacts["processing_profile"].as_string()
            candidate = candidate.where(
                or_(requested_profile.is_(None), requested_profile.in_(profiles))
            )
    result = await db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == candidate.scalar_subquery())
        .values(
            status=JobStatus.running,
            leased_by=body.worker_id,
            lease_token=uuid.uuid4(),
            control_requested=None,
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
    background: BackgroundTasks,
) -> JobResponse:
    """Extend a claimed job's lease and merge per-stage progress."""
    result = await db.execute(
        select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update()
    )
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

    _ensure_worker_lease(job, body.worker_id, body.lease_token, body.attempt_count)

    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=body.lease_seconds)
    if body.progress:
        _reject_json_bomb("progress", body.progress)
        segmented_before = _segment_succeeded(job.progress)
        merged = dict(job.progress or {})
        merged.update(body.progress)
        _reject_json_bomb("progress", merged)
        job.progress = merged
        # Game clips exist once segmentation is done; per-clip tracking can
        # take another hour, and coaches want the official results now.
        if (
            job.job_type == JobType.pipeline
            and job.video_id
            and not segmented_before
            and _segment_succeeded(merged)
        ):
            background.add_task(link_game_film, job.video_id)
    await db.flush()
    return JobResponse.from_orm_job(job)


def _segment_succeeded(progress: dict[str, Any] | None) -> bool:
    segment = (progress or {}).get("segment")
    return isinstance(segment, dict) and segment.get("status") == "succeeded"


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> JobResponse:
    """Get full job details including error reason, stage, and input file."""
    result = await db.execute(
        select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update()
    )
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
    background: BackgroundTasks,
) -> JobResponse:
    """Update job status — used by GPU worker callbacks."""
    result = await db.execute(
        select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update()
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if body.output_artifacts is not None:
        _reject_json_bomb("output_artifacts", body.output_artifacts)
    if body.progress is not None:
        _reject_json_bomb("progress", body.progress)
    if job.status in {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled}:
        if body.status == job.status:
            # Replayed completion is read-only; an old callback cannot replace
            # artifacts or error state after a newer attempt has finished.
            return JobResponse.from_orm_job(job)
        raise HTTPException(409, "Job is already finished")
    if body.lease_token is not None and body.lease_token != job.lease_token:
        raise HTTPException(409, "Stale worker attempt; lease token does not match")
    legacy_admin_cancel = (
        current_user.role == UserRole.admin
        and body.status == JobStatus.cancelled
        and not isinstance(job.lease_token, uuid.UUID)
    )
    if job.status == JobStatus.running and job.leased_by is not None and not legacy_admin_cancel:
        _ensure_worker_lease(job, body.worker_id, body.lease_token, body.attempt_count)
    allowed = {
        JobStatus.queued: {
            JobStatus.queued,
            JobStatus.running,
            JobStatus.cancelled,
            JobStatus.paused,
        },
        JobStatus.running: {
            JobStatus.running,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.paused,
        },
        JobStatus.paused: {JobStatus.paused, JobStatus.queued, JobStatus.cancelled},
        JobStatus.succeeded: {JobStatus.succeeded},
        JobStatus.failed: {JobStatus.failed},
        JobStatus.cancelled: {JobStatus.cancelled},
    }
    if body.status not in allowed[job.status]:
        raise HTTPException(409, f"Invalid job transition: {job.status} to {body.status}")
    if job.control_requested in {"cancel", "pause"} and body.status == JobStatus.succeeded:
        raise HTTPException(
            409, "Requested job control must be acknowledged before publishing success"
        )
    if body.output_artifacts is not None:
        _reject_json_bomb("output_artifacts", body.output_artifacts)
    if body.progress is not None:
        _reject_json_bomb("progress", body.progress)

    if body.provenance is not None:
        _reject_json_bomb("provenance", body.provenance)
        job.provenance = body.provenance
    if body.status == JobStatus.paused and job.control_requested == "cancel":
        body.status = JobStatus.cancelled
    if (
        body.status == JobStatus.paused
        and job.control_requested == "pause"
        and body.output_artifacts
    ):
        body.output_artifacts = {**body.output_artifacts, "interrupted_action": "pause"}
    job.status = body.status
    if body.status == JobStatus.running and job.started_at is None:
        job.started_at = datetime.now(UTC)
    if body.status in (
        JobStatus.succeeded,
        JobStatus.failed,
        JobStatus.cancelled,
        JobStatus.paused,
    ):
        job.finished_at = datetime.now(UTC)
        # Terminal states release the worker lease.
        job.leased_by = None
        job.lease_expires_at = None
        job.lease_token = None
        job.control_requested = None
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
    if body.status == JobStatus.succeeded and job.job_type == JobType.pipeline and job.video_id:
        background.add_task(link_game_film, job.video_id)
    return JobResponse.from_orm_job(job)


async def link_game_film(video_id: uuid.UUID) -> None:
    """Give a game film's clips their official CFBD results.

    Runs after the worker's request has been answered (its own session), so
    downloading the game's play-by-play never slows a heartbeat. Best effort
    and idempotent: it runs when segmentation finishes and again when the
    job succeeds; ``POST /api/cfbd/videos/{id}/link`` repeats it on demand.
    """
    from app.cfbd.game_link import ensure_plays, find_game, link_video
    from app.database import AsyncSessionLocal
    from app.models import SessionKind, Video

    try:
        async with AsyncSessionLocal() as db:
            video = await db.get(Video, video_id)
            if video is None or video.session_kind != SessionKind.game:
                return
            game = await find_game(db, video)
            if game is None:
                log.info("game_film_cfbd_link", video_id=str(video_id), status="no_game")
                return
            if not await ensure_plays(db, game):
                await db.commit()
                log.info("game_film_cfbd_link", video_id=str(video_id), status="no_plays")
                return
            result = await link_video(db, video_id)
            await db.commit()
        log.info("game_film_cfbd_link", video_id=str(video_id), **result)
    except Exception as exc:  # noqa: BLE001 - background work must never raise
        log.warning(
            "game_film_cfbd_link_failed", video_id=str(video_id), error_type=type(exc).__name__
        )


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
    result = await db.execute(
        select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update()
    )
    original = result.scalar_one_or_none()
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if original.status not in {JobStatus.failed, JobStatus.cancelled}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Only failed or cancelled jobs can be retried (current status: {original.status})"
            ),
        )

    if original.video_id:
        await db.execute(select(Video.id).where(Video.id == original.video_id).with_for_update())
        active = (
            await db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.video_id == original.video_id,
                    ProcessingJob.job_type == original.job_type,
                    ProcessingJob.status.in_(
                        [JobStatus.queued, JobStatus.running, JobStatus.paused]
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if active is not None and active.status in {
            JobStatus.queued,
            JobStatus.running,
            JobStatus.paused,
        }:
            return JobResponse.from_orm_job(active)
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


def _ensure_worker_lease(
    job: ProcessingJob,
    worker_id: str | None,
    lease_token: uuid.UUID | None,
    attempt_count: int | None,
) -> None:
    if job.leased_by != worker_id:
        raise HTTPException(409, "Worker lease does not match")
    token = job.lease_token
    if isinstance(token, uuid.UUID) and token != lease_token:
        raise HTTPException(409, "Stale worker attempt; lease token does not match")
    if attempt_count is not None and attempt_count != job.attempt_count:
        raise HTTPException(409, "Stale worker attempt")
    if (
        job.lease_expires_at
        and isinstance(job.lease_expires_at, datetime)
        and job.lease_expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(409, "Worker lease has expired")


def _set_interrupted_action(job: ProcessingJob, action: str | None) -> None:
    artifacts = dict(job.output_artifacts or {})
    if action is None:
        if "interrupted_action" not in artifacts:
            return
        artifacts.pop("interrupted_action")
    else:
        artifacts["interrupted_action"] = action
    job.output_artifacts = artifacts


async def _control_job(job_id: uuid.UUID, action: str, db: AsyncSession) -> JobResponse:
    job = (
        await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update())
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "Job not found")
    if action == "resume":
        if job.status != JobStatus.paused:
            raise HTTPException(409, "Only paused jobs can be resumed")
        job.status = JobStatus.queued
        job.control_requested = None
        job.finished_at = None
        # A deliberate pause is not a failed attempt.
        job.attempt_count = max(0, (job.attempt_count or 0) - 1)
        _set_interrupted_action(job, None)
    elif job.status == JobStatus.running:
        job.control_requested = action
    elif job.status in {JobStatus.queued, JobStatus.paused}:
        job.status = JobStatus.paused if action == "pause" else JobStatus.cancelled
        job.control_requested = None
        # An explicit coach control replaces any supervisor shutdown marker.
        _set_interrupted_action(job, action)
        if action == "cancel":
            job.finished_at = datetime.now(UTC)
    elif action == "cancel" and job.status == JobStatus.cancelled:
        pass
    else:
        raise HTTPException(409, "Job is already finished")
    await db.flush()
    return JobResponse.from_orm_job(job)


@router.post("/{job_id}/pause", response_model=JobResponse)
async def pause_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_any_staff)],
) -> JobResponse:
    return await _control_job(job_id, "pause", db)


@router.post("/{job_id}/resume", response_model=JobResponse)
async def resume_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_any_staff)],
) -> JobResponse:
    return await _control_job(job_id, "resume", db)


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_any_staff)],
) -> JobResponse:
    return await _control_job(job_id, "cancel", db)
