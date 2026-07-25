"""Reports router (Issue #111).

Replaces the client-side text-blob "report" with a real backend export
service.  Reports are generated in-process via FastAPI ``BackgroundTasks`` —
they're cheap aggregations and don't belong in the GPU pipeline's
pipeline that powers :mod:`app.routers.jobs`.

Endpoints:
    POST /api/v1/reports                — request a new report (coach+).
    GET  /api/v1/reports                — list reports (self for non-staff,
                                          everyone for admin/analyst).
    GET  /api/v1/reports/{id}           — single report status.
    GET  /api/v1/reports/{id}/download  — presigned download URL when ready.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import anyio
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal, get_db
from app.deps import get_current_user, require_coach_or_above
from app.models import JobStatus, ReportFormat, ReportJob, User, UserRole
from app.reports import (
    ALL_SECTIONS,
    REPORT_TYPE_COACHING_SUMMARY,
    aggregate_coaching_summary,
    render,
)
from app.storage import generate_download_url_for_uri, put_object

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: str = Field(default=REPORT_TYPE_COACHING_SUMMARY, max_length=64)
    format: ReportFormat = ReportFormat.pdf
    # Subset of ``app.reports.ALL_SECTIONS``. ``None`` includes all sections.
    sections: list[str] | None = None


class ReportResponse(BaseModel):
    id: uuid.UUID
    report_type: str
    format: str
    status: str
    parameters: dict[str, Any] | None
    output_uri: str | None
    error_message: str | None
    requested_by: uuid.UUID
    started_at: str | None
    finished_at: str | None
    created_at: str

    @classmethod
    def from_orm(cls, j: ReportJob) -> ReportResponse:
        return cls(
            id=j.id,
            report_type=j.report_type,
            format=j.format.value,
            status=j.status.value,
            parameters=j.parameters,
            output_uri=j.output_uri,
            error_message=j.error_message,
            requested_by=j.requested_by,
            started_at=j.started_at.isoformat() if j.started_at else None,
            finished_at=j.finished_at.isoformat() if j.finished_at else None,
            created_at=j.created_at.isoformat() if j.created_at else datetime.now(UTC).isoformat(),
        )


class ReportDownloadResponse(BaseModel):
    download_url: str
    expires_at: str


# ── Helpers ───────────────────────────────────────────────────────────────────


_STAFF_ROLES: frozenset[UserRole] = frozenset({UserRole.admin, UserRole.analyst})


def _can_see_all_reports(user: User) -> bool:
    return user.role in _STAFF_ROLES


def _ensure_can_access(user: User, job: ReportJob) -> None:
    if _can_see_all_reports(user):
        return
    if job.requested_by != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You may only access your own reports",
        )


async def _run_report_job(job_id: uuid.UUID) -> None:
    """Background task: generate the report, upload to the object store, update job row.

    Owns its own DB session because FastAPI's request-scoped session is no
    longer alive once the response has been sent.  Catches everything so a
    failure surfaces as ``status=failed`` rather than an unhandled task error.
    """
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ReportJob).where(ReportJob.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:  # pragma: no cover — defensive
            log.warning("report_job_missing", job_id=str(job_id))
            return

        job.status = JobStatus.running
        job.started_at = datetime.now(UTC)
        await db.commit()

        try:
            sections = None
            if job.parameters and isinstance(job.parameters.get("sections"), list):
                sections = [s for s in job.parameters["sections"] if isinstance(s, str)]
            data = await aggregate_coaching_summary(db, selected_sections=sections)
            body, content_type = render(data, job.format)

            ext = job.format.value
            key = f"reports/{job.id}.{ext}"
            uri = await anyio.to_thread.run_sync(
                put_object,
                settings.s3_bucket_artifacts,
                key,
                body,
                content_type,
            )

            job.output_uri = uri
            job.status = JobStatus.succeeded
        except Exception as exc:  # noqa: BLE001 — must convert to job-row failure
            log.exception("report_job_failed", job_id=str(job_id))
            job.status = JobStatus.failed
            job.error_message = str(exc)[:1000]
        finally:
            job.finished_at = datetime.now(UTC)
            await db.commit()


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    body: ReportCreate,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ReportResponse:
    """Request a new report; generation runs in the background."""
    if body.report_type != REPORT_TYPE_COACHING_SUMMARY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported report_type: {body.report_type!r}",
        )

    if body.sections is not None:
        if len(body.sections) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sections must not be empty",
            )
        unknown = [s for s in body.sections if s not in ALL_SECTIONS]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown sections: {unknown}",
            )

    parameters: dict[str, Any] = {
        "sections": list(body.sections) if body.sections else list(ALL_SECTIONS),
    }

    job = ReportJob(
        id=uuid.uuid4(),
        report_type=body.report_type,
        format=body.format,
        status=JobStatus.queued,
        parameters=parameters,
        requested_by=current_user.id,
    )
    db.add(job)
    await db.flush()
    log.info(
        "report_requested",
        job_id=str(job.id),
        report_type=body.report_type,
        format=body.format.value,
        requested_by=str(current_user.id),
    )

    background_tasks.add_task(_run_report_job, job.id)
    return ReportResponse.from_orm(job)


@router.get("", response_model=list[ReportResponse])
async def list_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ReportResponse]:
    """List the requesting user's reports newest-first.

    Admins and analysts see reports requested by any user.
    """
    q = select(ReportJob).order_by(ReportJob.created_at.desc()).limit(limit).offset(offset)
    if not _can_see_all_reports(current_user):
        q = q.where(ReportJob.requested_by == current_user.id)
    result = await db.execute(q)
    return [ReportResponse.from_orm(j) for j in result.scalars().all()]


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ReportResponse:
    result = await db.execute(select(ReportJob).where(ReportJob.id == report_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    _ensure_can_access(current_user, job)
    return ReportResponse.from_orm(job)


@router.get("/{report_id}/download", response_model=ReportDownloadResponse)
async def get_report_download_url(
    report_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ReportDownloadResponse:
    """Return a short-lived presigned URL for the generated artifact."""
    result = await db.execute(select(ReportJob).where(ReportJob.id == report_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    _ensure_can_access(current_user, job)

    if job.status != JobStatus.succeeded or not job.output_uri:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Report is not ready (status: {job.status.value})",
        )

    settings = get_settings()
    url = generate_download_url_for_uri(job.output_uri)
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.s3_presign_ttl)
    return ReportDownloadResponse(download_url=url, expires_at=expires_at.isoformat())
