"""Videos router — upload records, status, and presigned URL helpers."""

import hashlib
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import anyio
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import (
    CaptureRegime,
    FootballSession,
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

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/videos", tags=["videos"])

#: Enough for how a coach actually labels film; a bound stops the metadata bag
#: from becoming an unbounded write surface.
MAX_TAGS_PER_VIDEO = 20


async def _auto_process_config(db: AsyncSession) -> SystemConfig:
    """Read the set-and-forget toggle (defaults ON when no row exists)."""
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == SYSTEM_CONFIG_KEY))
    ).scalar_one_or_none()
    try:
        config = SystemConfig(**(row.value if row else {}))
    except Exception:
        config = SystemConfig()
    return config


async def _maybe_auto_enqueue_pipeline(db: AsyncSession, video: Video) -> ProcessingJob | None:
    """Persist upload intent in the durable queue, regardless of worker capacity.

    Capacity controls execution, never acceptance of already uploaded film.
    The caller commits the video and job together; a failed write is retryable.
    """
    config = await _auto_process_config(db)
    if not config.auto_process_on_upload:
        return None
    await db.execute(select(Video.id).where(Video.id == video.id).with_for_update())
    existing = (
        await db.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.video_id == video.id,
                ProcessingJob.job_type == JobType.pipeline,
                ProcessingJob.status.in_((JobStatus.queued, JobStatus.running, JobStatus.paused)),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.queued,
        priority=10 if config.auto_processing_profile == "quick" else 0,
        pipeline_mode=PipelineMode.same_session
        if config.auto_processing_profile == "quick"
        else PipelineMode.nightly,
        input_artifacts={
            "auto_enqueued": True,
            "processing_profile": config.auto_processing_profile,
            "auto_detailed_analysis": config.auto_detailed_analysis
            and config.auto_processing_profile == "quick",
        },
    )
    db.add(job)
    await db.flush()
    log.info("auto_process_enqueued", video_id=str(video.id), job_id=str(job.id))
    return job


# ── Schemas ───────────────────────────────────────────────────────────────────


class VideoCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    storage_uri: str
    recorded_at: datetime | None = None
    session_kind: SessionKind | None = None
    source_type: SourceType | None = None
    opponent_team: str | None = None
    practice_session_id: uuid.UUID | None = None
    library_role: Literal["toledo", "opponent"] = "toledo"
    camera_label: str | None = None
    our_possession: SideOfBall | None = None
    #: Free-form coach labels ("red zone", "install", "period 4"). Stored in the
    #: ``metadata`` bag rather than as a column: they are a browsing aid, not
    #: something any model or metric branches on.
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str] | None) -> list[str] | None:
        """Normalise to lowercase, de-duplicated, non-empty tags.

        Done server-side as well as in the UI so an API client cannot create
        ``Red Zone`` and ``red zone`` as two different browsing buckets.
        """
        if value is None:
            return None
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in value:
            tag = raw.strip().lower()[:60]
            if tag and tag not in seen:
                seen.add(tag)
                cleaned.append(tag)
        if len(cleaned) > MAX_TAGS_PER_VIDEO:
            raise ValueError(f"At most {MAX_TAGS_PER_VIDEO} tags per video")
        return cleaned or None

    @model_validator(mode="after")
    def _opponent_required_for_game(self) -> "VideoCreate":
        if self.session_kind in {SessionKind.practice, SessionKind.scrimmage}:
            self.opponent_team = None
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
    size_bytes: int | None = None
    content_sha256: str | None = None
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
    library_role: str = "toledo"
    camera_label: str | None = None
    our_possession: SideOfBall | None
    capture_regime: CaptureRegime | None
    regime_confidence: float | None
    metadata_: dict[str, Any] | None = None
    created_at: str
    # Registration-only outcome; never persisted on the shared recording.
    duplicate: bool = False

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_video(cls, v: Video) -> "VideoResponse":
        return cls(
            id=v.id,
            filename=v.filename,
            storage_uri=v.storage_uri,
            size_bytes=v.size_bytes if isinstance(v.size_bytes, int) else None,
            content_sha256=v.content_sha256 if isinstance(v.content_sha256, str) else None,
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
            library_role=v.library_role if isinstance(v.library_role, str) else "toledo",
            camera_label=v.camera_label if isinstance(v.camera_label, str) else None,
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
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
    library_role: Literal["toledo", "opponent"] | None = Query(default=None),
) -> list[VideoResponse]:
    """List videos with Hudl-style filters; ``recorded_at`` newest first."""
    q = (
        select(Video)
        .order_by(Video.recorded_at.desc().nullslast(), Video.created_at.desc(), Video.id)
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
    if library_role is not None:
        q = q.where(Video.library_role == library_role)
    result = await db.execute(q)
    videos = result.scalars().all()
    return [VideoResponse.from_orm_video(v) for v in videos]


@router.post("", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
async def create_video(
    body: VideoCreate,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_any_staff)],
) -> VideoResponse:
    """Register a new video record after the file has been uploaded to the object store."""
    # Storage URIs are validated before registration; local imports must point
    # to an atomically finalized object, never an arbitrary path or partial upload.
    from app.storage import (
        RAW_VIDEO,
        is_valid_key,
        local_object_path,
        parse_storage_uri,
        recording_fingerprint,
    )

    try:
        scheme, bucket, key = parse_storage_uri(body.storage_uri)
    except ValueError as exc:
        raise HTTPException(400, "Invalid recording storage URI") from exc
    if bucket != RAW_VIDEO or not is_valid_key(key):
        raise HTTPException(400, "Recording must use raw-video storage")
    if scheme == "local" and not local_object_path(bucket, key).is_file():
        raise HTTPException(409, "Recording upload is not finalized")
    try:
        content_sha256, size_bytes = await anyio.to_thread.run_sync(
            recording_fingerprint, body.storage_uri, get_settings().max_upload_bytes
        )
    except FileNotFoundError as exc:
        raise HTTPException(409, "Recording upload is not finalized") from exc
    except OverflowError as exc:
        raise HTTPException(413, "Recording exceeds the upload size limit") from exc
    except ValueError as exc:
        raise HTTPException(422, "Recording upload is empty or invalid") from exc
    except Exception as exc:
        log.warning("recording_verification_failed", error_type=type(exc).__name__)
        raise HTTPException(
            503, "Recording storage could not be verified. Retry saving film."
        ) from exc
    lock_id = int.from_bytes(hashlib.sha256(body.storage_uri.encode()).digest()[:8], "big") & (
        (1 << 63) - 1
    )
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))
    replay = (
        await db.execute(
            select(Video)
            .where(Video.storage_uri == body.storage_uri)
            .order_by(Video.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        isinstance(replay, Video)
        and replay.content_sha256
        and replay.content_sha256 != content_sha256
    ):
        raise HTTPException(409, "Recording bytes changed after registration. Use a new upload.")
    # A different upload key (or another authorized staff member) can contain
    # identical film. Serialize by verified bytes, then reuse the shared row.
    # The unique constraint also guards writers outside this router.
    content_lock = int(content_sha256[:16], 16) & ((1 << 63) - 1)
    await db.execute(select(func.pg_advisory_xact_lock(content_lock)))
    duplicate = (
        await db.execute(select(Video).where(Video.content_sha256 == content_sha256).limit(1))
    ).scalar_one_or_none()
    existing = duplicate if isinstance(duplicate, Video) else replay
    if isinstance(existing, Video):
        if not existing.content_sha256:
            existing.content_sha256 = content_sha256
            existing.size_bytes = size_bytes
            await db.flush()
        if existing.status == VideoStatus.uploaded:
            await _maybe_auto_enqueue_pipeline(db, existing)
        response.status_code = status.HTTP_200_OK
        return VideoResponse.from_orm_video(existing).model_copy(update={"duplicate": True})
    media = {}
    if scheme == "local":
        from app.services.media_probe import probe_local_video

        try:
            media = await anyio.to_thread.run_sync(
                probe_local_video, local_object_path(bucket, key)
            )
        except (ValueError, TimeoutError) as exc:
            raise HTTPException(422, "Recording cannot be read as video") from exc
    session_id = body.practice_session_id
    if session_id:
        session = (
            await db.execute(select(FootballSession).where(FootballSession.id == session_id))
        ).scalar_one_or_none()
        if session is None:
            raise HTTPException(404, "Session not found")
        # The workspace owns recording context. A camera upload cannot turn
        # one angle of our practice into an opponent game.
        if session.session_kind is not None:
            if body.session_kind is not None and body.session_kind != session.session_kind:
                raise HTTPException(422, "Recording session type must match its session")
            body.session_kind = session.session_kind
        body.library_role = "opponent" if session.library_role == "opponent" else "toledo"
        body.opponent_team = (
            None
            if body.session_kind in {SessionKind.practice, SessionKind.scrimmage}
            else session.opponent_team
        )
    else:
        session = FootballSession(
            id=uuid.uuid4(),
            title=body.filename[:255],
            session_kind=body.session_kind,
            recorded_at=body.recorded_at,
            opponent_team=body.opponent_team,
            library_role=body.library_role,
            created_by=current_user.id,
        )
        db.add(session)
        await db.flush()
        session_id = session.id
    video = Video(
        id=uuid.uuid4(),
        filename=body.filename,
        storage_uri=body.storage_uri,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        status=VideoStatus.uploaded,
        uploaded_by=current_user.id,
        recorded_at=body.recorded_at,
        session_kind=body.session_kind,
        source_type=body.source_type,
        opponent_team=body.opponent_team,
        practice_session_id=session_id,
        library_role=(
            body.library_role if "library_role" in body.model_fields_set else session.library_role
        ),
        camera_label=body.camera_label,
        duration_seconds=media.get("duration_seconds"),
        fps=media.get("fps"),
        width=media.get("width"),
        height=media.get("height"),
        codec=media.get("codec"),
        our_possession=body.our_possession,
        metadata_=(
            {
                **({"tags": body.tags} if body.tags else {}),
                **({"probe_state": media["probe_state"]} if media else {}),
            }
            or None
        ),
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
    await _maybe_auto_enqueue_pipeline(db, video)
    return VideoResponse.from_orm_video(video)


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
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


class VideoMetadataUpdate(BaseModel):
    recorded_at: datetime | None = None
    session_kind: SessionKind | None = None
    source_type: SourceType | None = None
    opponent_team: str | None = None
    practice_session_id: uuid.UUID | None = None
    library_role: Literal["toledo", "opponent"] | None = None
    camera_label: str | None = None
    our_possession: SideOfBall | None = None
    tags: list[str] | None = None


@router.patch("/{video_id}", response_model=VideoResponse)
async def update_video_metadata(
    video_id: uuid.UUID,
    body: VideoMetadataUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> VideoResponse:
    video = (
        await db.execute(select(Video).where(Video.id == video_id).with_for_update())
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(404, "Recording not found")
    changes = body.model_dump(exclude_unset=True)
    if (
        "practice_session_id" in changes
        and changes["practice_session_id"] != video.practice_session_id
    ):
        from app.routers.sessions import RecordingAssociation, associate_recording

        if changes["practice_session_id"] is None:
            raise HTTPException(
                409, "Move a recording to a session instead of removing its workspace"
            )
        await associate_recording(
            changes["practice_session_id"],
            RecordingAssociation(video_id=video_id, camera_label=body.camera_label),
            db,
            _user,
        )
    for key, value in changes.items():
        if key == "tags":
            video.metadata_ = {**(video.metadata_ or {}), "tags": VideoCreate._clean_tags(value)}
        else:
            setattr(video, key, value)
    await db.flush()
    return VideoResponse.from_orm_video(video)
