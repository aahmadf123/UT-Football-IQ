"""Fence result publication in the same transaction as worker output writes."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import JobStatus, ProcessingJob


async def validate_worker_write(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    job_header = request.headers.get("x-job-id")
    if request.method not in {"POST", "PATCH", "PUT", "DELETE"} or not job_header:
        return
    try:
        job_id = uuid.UUID(job_header)
        token = uuid.UUID(request.headers.get("x-job-lease-token", ""))
    except ValueError as exc:
        raise HTTPException(409, "Worker attempt headers are invalid") from exc
    row = (
        await db.execute(select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update())
    ).scalar_one_or_none()
    if (
        row is None
        or row.status != JobStatus.running
        or row.lease_token != token
        or row.leased_by != request.headers.get("x-worker-id")
        or (row.lease_expires_at and row.lease_expires_at <= datetime.now(UTC))
    ):
        raise HTTPException(409, "Worker attempt is no longer current")
    if row.control_requested == "cancel":
        raise HTTPException(409, "Worker output publication cancelled")
