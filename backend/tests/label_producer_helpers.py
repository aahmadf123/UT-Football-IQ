"""HTTP-only publication of explicitly protocol fixture predictions."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Clip, FieldCalibration, JobStatus, JobType, ProcessingJob, WorkerRuntime
from app.storage import local_object_path, parse_storage_uri
from httpx import ASGITransport, AsyncClient


async def publish_pipeline_predictions(db, processor, video, model, labels, monkeypatch):
    """A current configured processor publishes actual fixture bytes over HTTP."""
    monkeypatch.setenv("SEED_WORKER_EMAIL", processor.email)
    # Official game linking is independent of this producer-authentication fixture.
    monkeypatch.setattr("app.routers.jobs.link_game_film", AsyncMock())
    get_settings.cache_clear()
    video.content_sha256, video.size_bytes = "a" * 64, 1024
    # Explicit numerical calibration context for this protocol-only HTTP fixture.
    calibration = FieldCalibration(
        id=uuid.uuid4(),
        video_id=video.id,
        homography=[1, 0, 0, 0, 1, 0, 0, 0, 1],
        confidence=0.95,
        confidence_threshold=0.7,
        analytics_safe=True,
        reason_codes=[],
        provenance={"source": "human", "protocol_fixture": True},
    )
    db.add(calibration)
    await db.flush()
    for label in labels:
        clip = await db.get(Clip, label.clip_id)
        if clip.calibration_version_id is None:
            clip.calibration_version_id = calibration.id
    scheme, bucket, key = parse_storage_uri(model.artifact_uri)
    assert scheme == "local"
    artifact = hashlib.sha256(local_object_path(bucket, key).read_bytes()).hexdigest()
    model.metrics = {**(model.metrics or {}), "artifact_sha256": artifact}
    worker_id, token = f"label-{uuid.uuid4()}", uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by=worker_id,
        lease_token=token,
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add_all([job, WorkerRuntime(worker_id=worker_id, registered_by=processor.id)])
    await db.flush()
    source = {
        "schema_version": 1,
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "size_bytes": video.size_bytes,
        "registration_fingerprint": video.content_sha256,
        "registration_algorithm": "full-sha256",
        "full_sha256": video.content_sha256,
    }
    previous = dict(app.dependency_overrides)

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    app.dependency_overrides[get_current_user] = lambda: processor
    headers = {"X-Job-ID": str(job.id), "X-Worker-ID": worker_id, "X-Job-Lease-Token": str(token)}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            early = await client.patch(
                f"/api/v1/jobs/{job.id}",
                json={
                    "status": "running",
                    "worker_id": worker_id,
                    "lease_token": str(token),
                    "attempt_count": 1,
                    "output_artifacts": {"source_identity": source},
                },
            )
            assert early.status_code == 200, early.text
            for label in labels:
                response = await client.post(
                    "/api/v1/labels",
                    headers=headers,
                    json={
                        "id": str(label.id),
                        "clip_id": str(label.clip_id),
                        "tracklet_id": str(label.tracklet_id) if label.tracklet_id else None,
                        "label_type": label.label_type,
                        "source": "model",
                        "job_id": str(job.id),
                        "model_version_id": str(model.id),
                        "label_value": {
                            **label.label_value,
                            "provenance": {
                                "source": "real",
                                "job_id": str(job.id),
                                "artifact_sha256": artifact,
                            },
                        },
                    },
                )
                assert response.status_code == 201, response.text
            completed = await client.patch(
                f"/api/v1/jobs/{job.id}",
                json={
                    "status": "succeeded",
                    "worker_id": worker_id,
                    "lease_token": str(token),
                    "attempt_count": 1,
                    "output_artifacts": {"source_identity": source},
                },
            )
            assert completed.status_code == 200, completed.text
            from app.models import Label
            from app.services.label_publication import require_label_proof

            for label in labels:
                await require_label_proof(db, await db.get(Label, label.id), model)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
    return job


async def evaluate_and_promote(db, actor, model, benchmark_id, prediction_ids):
    previous = dict(app.dependency_overrides)

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    app.dependency_overrides[get_current_user] = lambda: actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            evaluated = await client.post(
                "/api/v1/learning/evaluations",
                json={
                    "model_id": str(model.id),
                    "benchmark_id": str(benchmark_id),
                    "prediction_label_ids": [str(value) for value in prediction_ids],
                },
            )
            assert evaluated.status_code == 201, evaluated.text
            assert evaluated.json()["passed"] is True
            promoted = await client.post(
                f"/api/v1/mlops/models/{model.id}/promote", json={"stage": "production"}
            )
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["promoted_stage"] == "production"
            return evaluated.json()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
