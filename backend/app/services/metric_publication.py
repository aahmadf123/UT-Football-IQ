"""Authenticate the producer of approved automatic metric values."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import JobStatus, JobType, ProcessingJob, User, WorkerRuntime
from app.services.expected_outcomes import trusted_worker


async def trusted_worker_job(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    actor: Annotated[User, Depends(get_current_user)],
    *,
    allow_training: bool = False,
) -> ProcessingJob | None:
    """A staff role or copied lease alone is not proof of worker publication."""
    header = request.headers.get("x-job-id")
    if not header:
        return None
    try:
        job_id = uuid.UUID(header)
        token = uuid.UUID(request.headers.get("x-job-lease-token", ""))
    except ValueError as exc:
        raise HTTPException(409, "Worker attempt headers are invalid") from exc
    job = (
        await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update())
    ).scalar_one_or_none()
    worker_id = request.headers.get("x-worker-id")
    runtime = await db.get(WorkerRuntime, worker_id) if worker_id else None
    if (
        not trusted_worker(actor, runtime)
        or job is None
        or job.job_type
        not in ({JobType.pipeline, JobType.train} if allow_training else {JobType.pipeline})
        or job.status != JobStatus.running
        or job.lease_token != token
        or job.leased_by != worker_id
        or not job.lease_expires_at
        or job.lease_expires_at <= datetime.now(UTC)
        or job.control_requested == "cancel"
    ):
        # Staff may submit observations and experimental candidates. Only a
        # positively authenticated processor can qualify automatic production
        # values; the router enforces that boundary after model/evidence checks.
        return None
    return job


async def trusted_metric_job(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    actor: Annotated[User, Depends(get_current_user)],
) -> ProcessingJob | None:
    return await trusted_worker_job(request, db, actor)


async def trusted_label_job(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    actor: Annotated[User, Depends(get_current_user)],
) -> ProcessingJob | None:
    return await trusted_worker_job(request, db, actor, allow_training=True)
