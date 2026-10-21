"""Full source identities attested by a registered, fenced processor.

Large-film registration uses a sampled fingerprint. It is useful for upload
deduplication but cannot stand in for a full-file training or correction hash.
These server-owned records are deliberately outside editable video metadata.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import JobStatus, JobType, ProcessingJob, SystemSetting, User, Video, WorkerRuntime
from app.storage import FULL_HASH_BYTES


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _registration(video: Video) -> dict[str, Any] | None:
    if (
        not _sha(video.content_sha256)
        or type(video.size_bytes) is not int
        or video.size_bytes <= 0
        or not isinstance(video.storage_uri, str)
        or not video.storage_uri
    ):
        return None
    return {
        "schema_version": 1,
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "size_bytes": video.size_bytes,
        "registration_fingerprint": video.content_sha256,
        "registration_algorithm": "full-sha256"
        if video.size_bytes <= FULL_HASH_BYTES
        else "sampled-sha256-v1",
    }


async def verified_media_sha256(db: AsyncSession, video: Video) -> str | None:
    registration = _registration(video)
    if registration is None:
        return None
    if registration["registration_algorithm"] == "full-sha256":
        return str(registration["registration_fingerprint"])
    record = await db.get(SystemSetting, f"media:verified:{video.id}")
    proof = record.value if record is not None and isinstance(record.value, dict) else {}
    receipt = proof.get("source_identity")
    if (
        record is None
        or not isinstance(receipt, dict)
        or any(receipt.get(key) != value for key, value in registration.items())
        or not _sha(receipt.get("full_sha256"))
        or not _sha(proof.get("lease_sha256"))
        or not proof.get("worker_id")
        or not record.updated_by
    ):
        return None
    try:
        job_id = uuid.UUID(str(proof["job_id"]))
    except (ValueError, KeyError, TypeError):
        return None
    job = await db.get(ProcessingJob, job_id)
    if (
        job is None
        or job.job_type != JobType.pipeline
        or job.video_id != video.id
        or type(proof.get("attempt_count")) is not int
        or proof["attempt_count"] < 1
    ):
        return None
    return str(receipt["full_sha256"])


async def verified_job_media_identity(
    db: AsyncSession, job: ProcessingJob
) -> dict[str, Any] | None:
    """A completed exporting job must have its own trusted fenced attestation."""
    if job.status != JobStatus.succeeded or job.job_type != JobType.pipeline or not job.video_id:
        return None
    record = await db.get(SystemSetting, f"media:attestation:{job.id}")
    proof = record.value if record is not None and isinstance(record.value, dict) else {}
    receipt = proof.get("source_identity")
    if (
        record is None
        or not record.updated_by
        or proof.get("job_id") != str(job.id)
        or proof.get("attempt_count") != job.attempt_count
        or not _sha(proof.get("lease_sha256"))
        or not proof.get("worker_id")
        or not isinstance(receipt, dict)
        or (job.output_artifacts or {}).get("source_identity") != receipt
    ):
        return None
    video = await db.get(Video, job.video_id)
    if video is None:
        return None
    registration = _registration(video)
    if (
        registration is None
        or any(receipt.get(key) != value for key, value in registration.items())
        or receipt.get("full_sha256") != await verified_media_sha256(db, video)
    ):
        return None
    return dict(receipt)


async def bind_media_identity_completion(
    db: AsyncSession,
    job: ProcessingJob,
    actor: User,
    output: dict[str, Any],
    *,
    worker_id: str | None,
    lease_token: uuid.UUID | None,
    attempt_count: int | None,
) -> None:
    receipt = output.get("source_identity")
    if receipt is None:
        return  # Historical processors can complete without granting identity proof.
    configured = get_settings().seed_worker_email.strip().casefold()
    runtime = await db.get(WorkerRuntime, worker_id) if worker_id else None
    if (
        not configured
        or actor.email.casefold() != configured
        or runtime is None
        or runtime.registered_by != actor.id
        or job.job_type != JobType.pipeline
        or job.status != JobStatus.running
        or not isinstance(job.lease_token, uuid.UUID)
        or job.lease_token != lease_token
        or job.leased_by != worker_id
        or job.attempt_count != attempt_count
        or not job.lease_expires_at
        or job.lease_expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(403, "Source identity requires the trusted current processor lease")
    video = await db.get(Video, job.video_id)
    if video is None:
        raise HTTPException(422, "Processor source recording is missing")
    registration = _registration(video)
    if (
        registration is None
        or not isinstance(receipt, dict)
        or type(receipt.get("schema_version")) is not int
        or set(receipt) != {*registration, "full_sha256"}
        or any(receipt.get(key) != value for key, value in registration.items())
        or type(receipt.get("size_bytes")) is not int
        or not _sha(receipt.get("full_sha256"))
        or (
            registration["registration_algorithm"] == "full-sha256"
            and receipt["full_sha256"] != video.content_sha256
        )
    ):
        raise HTTPException(
            422, "Processor source identity does not match the registered recording"
        )
    # Source bytes were attested under this live lease. The immutable proof
    # survives later analysis failures/retries without granting model approval.
    # Serialize publications for one source before creating its immutable proof.
    await db.execute(select(Video.id).where(Video.id == video.id).with_for_update())
    key = f"media:verified:{video.id}"
    existing = await db.get(SystemSetting, key)
    if existing is not None:
        if existing.value.get("source_identity") != receipt:
            raise HTTPException(
                409, "Recording content changed; register the new source separately"
            )
    proof = {
        "source_identity": receipt,
        "job_id": str(job.id),
        "worker_id": worker_id,
        "attempt_count": attempt_count,
        "lease_sha256": hashlib.sha256(str(lease_token).encode()).hexdigest(),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    if existing is None:
        db.add(SystemSetting(key=key, updated_by=actor.id, value=proof))
    # Each later processing job/attempt proves its own source read, without
    # replacing the recording's original immutable content identity.
    attempt_key = f"media:attestation:{job.id}"
    attestation = await db.get(SystemSetting, attempt_key)
    if attestation is None:
        db.add(SystemSetting(key=attempt_key, updated_by=actor.id, value=proof))
    else:
        attestation.value, attestation.updated_by = proof, actor.id
    await db.flush()
