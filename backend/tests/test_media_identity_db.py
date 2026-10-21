"""Real API callback transaction publishes identity before metric ingest."""

import uuid
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.models import JobStatus, JobType, ProcessingJob, WorkerRuntime
from app.routers.jobs import JobStatusUpdate, update_job_status
from app.services.media_identity import verified_media_sha256
from app.storage import FULL_HASH_BYTES
from fastapi import BackgroundTasks
from test_platform_overhaul import actor, db, recording  # noqa: F401 -- shared rollback fixture


async def test_running_fenced_callback_makes_large_source_identity_available(db, monkeypatch):  # noqa: F811
    user = await actor(db)
    monkeypatch.setenv("SEED_WORKER_EMAIL", user.email)
    get_settings.cache_clear()
    video, _ = await recording(db)
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, FULL_HASH_BYTES + 100
    worker_id = f"identity-{uuid.uuid4()}"
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by=worker_id,
        attempt_count=1,
        lease_token=uuid.uuid4(),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add_all([job, WorkerRuntime(worker_id=worker_id, registered_by=user.id)])
    await db.flush()
    full_hash = uuid.uuid4().hex * 2
    receipt = {
        "schema_version": 1,
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "size_bytes": video.size_bytes,
        "registration_fingerprint": video.content_sha256,
        "registration_algorithm": "sampled-sha256-v1",
        "full_sha256": full_hash,
    }
    assert await verified_media_sha256(db, video) is None
    result = await update_job_status(
        job.id,
        JobStatusUpdate(
            status=JobStatus.running,
            output_artifacts={"source_identity": receipt},
            worker_id=worker_id,
            lease_token=job.lease_token,
            attempt_count=1,
        ),
        db,
        user,
        BackgroundTasks(),
    )
    assert result.status == "running"
    assert await verified_media_sha256(db, video) == full_hash
    # Retrying analysis never turns the already hashed source back into a
    # sampled hash, nor does this proof promote any prediction/model.
    job.status, job.attempt_count, job.output_artifacts = JobStatus.failed, 2, {}
    await db.flush()
    assert await verified_media_sha256(db, video) == full_hash
