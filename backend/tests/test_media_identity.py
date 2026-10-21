"""Registration fingerprints never impersonate full-content training identities."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.models import JobStatus, JobType, ProcessingJob, SystemSetting, User, Video, WorkerRuntime
from app.services.media_identity import (
    bind_media_identity_completion,
    verified_job_media_identity,
    verified_media_sha256,
)
from app.storage import FULL_HASH_BYTES
from fastapi import HTTPException


def fixture():
    actor = User(id=uuid.uuid4(), email="processor@example.test")
    video = Video(
        id=uuid.uuid4(),
        storage_uri="local://raw-video/game.mp4",
        size_bytes=FULL_HASH_BYTES + 1,
        content_sha256="a" * 64,
    )
    token = uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by="worker",
        lease_token=token,
        attempt_count=2,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    runtime = WorkerRuntime(worker_id="worker", registered_by=actor.id)
    receipt = {
        "schema_version": 1,
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "size_bytes": video.size_bytes,
        "registration_fingerprint": video.content_sha256,
        "registration_algorithm": "sampled-sha256-v1",
        "full_sha256": "b" * 64,
    }
    records = {}
    db = AsyncMock()
    db.get.side_effect = lambda cls, key: (
        {Video: video, ProcessingJob: job, WorkerRuntime: runtime}.get(cls)
        if cls is not SystemSetting
        else records.get(key)
    )
    db.add = Mock(side_effect=lambda value: records.update({value.key: value}))
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_small_registration_is_full_hash_but_large_and_unknown_are_not():
    f = fixture()
    assert await verified_media_sha256(f.db, f.video) is None
    f.video.size_bytes = None
    assert await verified_media_sha256(f.db, f.video) is None
    f.video.size_bytes = FULL_HASH_BYTES
    assert await verified_media_sha256(f.db, f.video) == "a" * 64


@pytest.mark.asyncio
async def test_fenced_processor_receipt_enables_large_media_identity(monkeypatch):
    monkeypatch.setenv("SEED_WORKER_EMAIL", "processor@example.test")
    f = fixture()
    output = {"source_identity": f.receipt}
    await bind_media_identity_completion(
        f.db, f.job, f.actor, output, worker_id="worker", lease_token=f.token, attempt_count=2
    )
    f.job.output_artifacts = output
    assert await verified_media_sha256(f.db, f.video) == "b" * 64
    f.job.status = JobStatus.succeeded
    f.job.output_artifacts = output
    assert await verified_media_sha256(f.db, f.video) == "b" * 64
    assert await verified_job_media_identity(f.db, f.job) == f.receipt
    f.job.attempt_count += 1
    assert await verified_job_media_identity(f.db, f.job) is None
    f.video.storage_uri = "local://raw-video/other.mp4"
    assert await verified_media_sha256(f.db, f.video) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "actor",
        "runtime",
        "lease",
        "attempt",
        "expired",
        "video_id",
        "storage_uri",
        "size_bytes",
        "registration_fingerprint",
        "registration_algorithm",
        "full_sha256",
    ],
)
async def test_untrusted_or_mismatched_receipt_is_rejected(monkeypatch, fault):
    monkeypatch.setenv("SEED_WORKER_EMAIL", "processor@example.test")
    f = fixture()
    if fault == "actor":
        f.actor.email = "staff@example.test"
    elif fault == "runtime":
        f.runtime.registered_by = uuid.uuid4()
    elif fault == "lease":
        f.token = uuid.uuid4()
    elif fault == "attempt":
        f.job.attempt_count = 3
    elif fault == "expired":
        f.job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        f.receipt[fault] = "foreign"
    with pytest.raises(HTTPException):
        await bind_media_identity_completion(
            f.db,
            f.job,
            f.actor,
            {"source_identity": f.receipt},
            worker_id="worker",
            lease_token=f.token,
            attempt_count=2,
        )
    assert not f.records


@pytest.mark.asyncio
async def test_metadata_cannot_supply_identity_and_finished_receipt_must_match():
    f = fixture()
    f.video.metadata_ = {"verified_full_sha256": "b" * 64, "source_identity": f.receipt}
    assert await verified_media_sha256(f.db, f.video) is None


@pytest.mark.asyncio
async def test_later_processing_job_attests_without_replacing_original_content_proof(monkeypatch):
    monkeypatch.setenv("SEED_WORKER_EMAIL", "processor@example.test")
    f = fixture()
    original_id = f.job.id
    await bind_media_identity_completion(
        f.db,
        f.job,
        f.actor,
        {"source_identity": f.receipt},
        worker_id="worker",
        lease_token=f.token,
        attempt_count=2,
    )
    f.job.id = uuid.uuid4()
    await bind_media_identity_completion(
        f.db,
        f.job,
        f.actor,
        {"source_identity": f.receipt},
        worker_id="worker",
        lease_token=f.token,
        attempt_count=2,
    )
    assert f.records[f"media:verified:{f.video.id}"].value["job_id"] == str(original_id)
    assert f.records[f"media:attestation:{f.job.id}"].value["job_id"] == str(f.job.id)
    f.job.status, f.job.output_artifacts = JobStatus.succeeded, {"source_identity": f.receipt}
    assert await verified_job_media_identity(f.db, f.job) == f.receipt


@pytest.mark.asyncio
async def test_existing_full_source_proof_does_not_attest_another_jobs_export(monkeypatch):
    monkeypatch.setenv("SEED_WORKER_EMAIL", "processor@example.test")
    f = fixture()
    await bind_media_identity_completion(
        f.db,
        f.job,
        f.actor,
        {"source_identity": f.receipt},
        worker_id="worker",
        lease_token=f.token,
        attempt_count=2,
    )
    f.job.id = uuid.uuid4()
    f.job.status, f.job.output_artifacts = JobStatus.succeeded, {"source_identity": f.receipt}
    assert await verified_media_sha256(f.db, f.video) == "b" * 64
    assert await verified_job_media_identity(f.db, f.job) is None
