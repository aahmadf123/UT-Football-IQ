"""Durable, idempotent fast-to-detailed processing over the existing DB queue."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobStatus, JobType, PipelineMode, ProcessingJob, Video


async def schedule_detailed_followup(db: AsyncSession, job: ProcessingJob) -> ProcessingJob | None:
    inputs = job.input_artifacts or {}
    if (
        job.status != JobStatus.succeeded
        or job.job_type != JobType.pipeline
        or not job.video_id
        or inputs.get("processing_profile") != "quick"
        or inputs.get("auto_enqueued") is not True
        or inputs.get("auto_detailed_analysis") is not True
        or inputs.get("detailed_followup_of")
    ):
        return None
    # Completion holds the parent job lock; recording lock also serializes
    # competing requests. No online worker or free queue capacity is required.
    await db.execute(select(Video.id).where(Video.id == job.video_id).with_for_update())
    followup_id = job.nightly_followup_job_id or uuid.uuid5(
        uuid.NAMESPACE_URL, f"footiq:detailed:{job.id}"
    )
    existing = await db.get(ProcessingJob, followup_id)
    if existing is not None:
        return existing
    followup = ProcessingJob(
        id=followup_id,
        video_id=job.video_id,
        job_type=JobType.pipeline,
        status=JobStatus.queued,
        priority=0,
        pipeline_mode=PipelineMode.nightly,
        input_artifacts={
            **inputs,
            "processing_profile": "detailed",
            "detailed_followup_of": str(job.id),
            "auto_detailed_analysis": False,
        },
    )
    job.nightly_followup_job_id = followup.id
    db.add(followup)
    await db.flush()
    return followup
