"""Actual HTTP label execution-fence contracts; protocol fixtures, no accuracy evidence."""

# ruff: noqa: F811 -- imported rollback fixture shares pytest parameter names

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    FieldCalibration,
    JobStatus,
    JobType,
    Label,
    ModelVersion,
    ProcessingJob,
    SystemSetting,
    TrainingDataset,
    WorkerRuntime,
)
from app.services.label_publication import require_label_proof
from app.storage import local_object_path
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from label_producer_helpers import publish_pipeline_predictions
from test_platform_overhaul import actor, db, player_track, recording  # noqa: F401


async def fixture(db, monkeypatch, tmp_path):
    processor, staff = await actor(db), await actor(db)
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("SEED_WORKER_EMAIL", processor.email)
    get_settings.cache_clear()
    video, clip = await recording(db)
    video.content_sha256, video.size_bytes = "a" * 64, 1024
    video.capture_regime = clip.capture_regime = "fixed_sideline"
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
    clip.calibration_version_id = calibration.id
    path = local_object_path("artifacts", "models/fixture.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"schema_version":1,"protocol":"not football accuracy"}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    model = ModelVersion(
        id=uuid.uuid4(),
        model_name=f"label-protocol-{uuid.uuid4()}",
        version="fixture",
        model_type="coverage",
        artifact_uri="local://artifacts/models/fixture.json",
        metrics={"artifact_sha256": digest},
    )
    worker = f"label-{uuid.uuid4()}"
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by=worker,
        lease_token=uuid.uuid4(),
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add_all([model, job, WorkerRuntime(worker_id=worker, registered_by=processor.id)])
    await db.flush()
    body = {
        "id": str(uuid.uuid4()),
        "clip_id": str(clip.id),
        "label_type": "coverage_shell",
        "source": "model",
        "job_id": str(job.id),
        "model_version_id": str(model.id),
        "label_value": {
            "coverage": "cover_1",
            "provenance": {"source": "real", "artifact_sha256": digest, "job_id": str(job.id)},
        },
    }
    headers = {
        "X-Job-ID": str(job.id),
        "X-Worker-ID": worker,
        "X-Job-Lease-Token": str(job.lease_token),
    }
    return processor, staff, video, clip, model, job, body, headers


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "ordinary",
        "copied_fence",
        "body_job",
        "artifact",
        "model_task",
        "foreign_video",
        "foreign_clip",
        "foreign_track",
        "expired",
        "training_job",
        "synthetic_wrapper",
        "calibration_missing",
        "calibration_unsafe",
        "calibration_foreign",
        "calibration_singular",
        "exact_retry",
        "changed_retry",
    ],
)
async def test_only_current_actual_processor_can_attest_real_model_label(
    db, monkeypatch, tmp_path, fault
):  # noqa: F811
    processor, staff, video, clip, model, job, body, headers = await fixture(
        db, monkeypatch, tmp_path
    )
    principal = staff if fault in {"ordinary", "copied_fence"} else processor
    if fault == "ordinary":
        headers = {}
    if fault == "body_job":
        body["job_id"] = str(uuid.uuid4())
    if fault == "artifact":
        body["label_value"]["provenance"]["artifact_sha256"] = "f" * 64
    if fault == "model_task":
        model.model_type = "route"
    if fault == "foreign_video":
        other, _ = await recording(db)
        job.video_id = other.id
    if fault == "foreign_clip":
        job.clip_id = (await recording(db))[1].id
    if fault == "foreign_track":
        body["tracklet_id"] = str((await player_track(db, (await recording(db))[1])).id)
    if fault == "expired":
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if fault == "training_job":
        job.job_type = JobType.train
    if fault == "synthetic_wrapper":
        body["label_value"]["source"] = "synthetic_test"
    if fault.startswith("calibration_") if isinstance(fault, str) else False:
        row = await db.get(FieldCalibration, clip.calibration_version_id)
        if fault == "calibration_missing":
            clip.calibration_version_id = None
        if fault == "calibration_unsafe":
            row.analytics_safe = False
        if fault == "calibration_foreign":
            row.video_id = (await recording(db))[0].id
        if fault == "calibration_singular":
            row.homography = [0] * 9
    await db.flush()
    previous = dict(app.dependency_overrides)

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    app.dependency_overrides[get_current_user] = lambda: principal
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/labels", json=body, headers=headers)
            if fault in {None, "exact_retry", "changed_retry"}:
                assert response.status_code == 201, response.text
                assert await db.get(SystemSetting, f"labels:producer:{body['id']}") is not None
                if fault == "changed_retry":
                    body["label_value"]["coverage"] = "cover_3"
                if fault:
                    replay = await client.post("/api/v1/labels", json=body, headers=headers)
                    assert replay.status_code == (409 if fault == "changed_retry" else 201), (
                        replay.text
                    )
            else:
                assert response.status_code in {403, 409, 422}, response.text
                assert await db.get(SystemSetting, f"labels:producer:{body['id']}") is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize(
    "mutation",
    [
        "failed",
        "running",
        "attempt",
        "body",
        "source",
        "source_path",
        "artifact",
        "copied_label",
        "output",
        "proof_missing",
        "clip_regime",
        "video_regime",
        "calibration",
    ],
)
async def test_evaluation_rejects_unproven_changed_or_incomplete_labels(
    db, monkeypatch, tmp_path, mutation
):  # noqa: F811
    processor, _staff, video, clip, model, _job, _body, _headers = await fixture(
        db, monkeypatch, tmp_path
    )
    label = Label(
        id=uuid.uuid4(),
        clip_id=clip.id,
        label_type="coverage_shell",
        label_value={"coverage": "cover_1"},
        source="model",
        model_version_id=model.id,
    )
    job = await publish_pipeline_predictions(db, processor, video, model, [label], monkeypatch)
    label = await db.get(Label, label.id)
    await require_label_proof(db, label, model)
    assert "publication_holds" in job.output_artifacts  # Server-added final document is sealed.
    if mutation == "failed":
        job.status = JobStatus.failed
    if mutation == "running":
        job.status = JobStatus.running
    if mutation == "attempt":
        job.attempt_count += 1
    if mutation == "body":
        label.label_value = {"coverage": "cover_3", "provenance": label.label_value["provenance"]}
    if mutation == "source":
        video.content_sha256 = "b" * 64
    if mutation == "source_path":
        video.storage_uri = "local://raw-video/different.mp4"
    if mutation == "artifact":
        local_object_path("artifacts", "models/fixture.json").write_bytes(b"different")
    if mutation == "clip_regime":
        clip.capture_regime = "drone_follow"
    if mutation == "video_regime":
        video.capture_regime = "drone_follow"
    if mutation == "calibration":
        (await db.get(FieldCalibration, clip.calibration_version_id)).confidence = 0.96
    if mutation == "output":
        job.output_artifacts = {**job.output_artifacts, "forged": True}
    if mutation == "proof_missing":
        await db.delete(await db.get(SystemSetting, f"labels:producer:{label.id}"))
    if mutation == "copied_label":
        original = await db.get(SystemSetting, f"labels:producer:{label.id}")
        copy = Label(
            id=uuid.uuid4(),
            clip_id=clip.id,
            label_type=label.label_type,
            label_value=label.label_value,
            source="model",
            model_version_id=model.id,
        )
        db.add(copy)
        db.add(
            SystemSetting(
                key=f"labels:producer:{copy.id}", value=original.value, updated_by=processor.id
            )
        )
        label = copy
    await db.flush()
    with pytest.raises(HTTPException, match="trusted producer proof"):
        await require_label_proof(db, label, model)


async def test_copied_fence_cannot_complete_a_real_label_producer(db, monkeypatch, tmp_path):
    processor, staff, _video, _clip, model, job, body, headers = await fixture(
        db, monkeypatch, tmp_path
    )
    previous = dict(app.dependency_overrides)

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    app.dependency_overrides[get_current_user] = lambda: processor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/labels", json=body, headers=headers)
            assert response.status_code == 201, response.text
            app.dependency_overrides[get_current_user] = lambda: staff
            completed = await client.patch(
                f"/api/v1/jobs/{job.id}",
                json={
                    "status": "succeeded",
                    "worker_id": job.leased_by,
                    "lease_token": str(job.lease_token),
                    "attempt_count": job.attempt_count,
                    "output_artifacts": {},
                },
            )
            assert completed.status_code == 403
            assert job.status == JobStatus.running
            with pytest.raises(HTTPException, match="trusted producer proof"):
                await require_label_proof(db, await db.get(Label, uuid.UUID(body["id"])), model)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def test_api_training_retry_preserves_exact_dataset_scope(db, monkeypatch, tmp_path):
    processor, _staff, _video, _clip, _model, job, _body, _headers = await fixture(
        db, monkeypatch, tmp_path
    )
    dataset = TrainingDataset(
        id=uuid.uuid4(),
        model_scope="coverage",
        row_count=0,
        source_label_ids=[],
        source_correction_ids=[],
        artifact_uri="local://artifacts/train.jsonl",
    )
    db.add(dataset)
    await db.flush()
    job.job_type, job.status, job.video_id = JobType.train, JobStatus.failed, None
    job.training_dataset_id = dataset.id
    job.input_artifacts = {
        "training_variant": "local_coverage",
        "training_dataset_id": str(dataset.id),
    }
    await db.flush()
    monkeypatch.setenv("WORKLOAD_GATING_DISABLED", "1")
    get_settings.cache_clear()
    previous = dict(app.dependency_overrides)

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    app.dependency_overrides[get_current_user] = lambda: processor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/jobs/{job.id}/retry")
            assert response.status_code == 201, response.text
            retry = await db.get(ProcessingJob, uuid.UUID(response.json()["id"]))
            assert retry.id != job.id and retry.training_dataset_id == dataset.id
            assert retry.input_artifacts == job.input_artifacts
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "alternate_dataset",
        "non_holdout",
        "model_job",
        "foreign_model",
        "source_change",
        "frozen_hash",
        "wrong_type",
        "failed_source",
    ],
)
async def test_candidate_proof_requires_exact_frozen_holdout_scope(
    db, monkeypatch, tmp_path, fault
):
    processor, _staff, video, clip, model, job, body, headers = await fixture(
        db, monkeypatch, tmp_path
    )
    source_job = await publish_pipeline_predictions(db, processor, video, model, [], monkeypatch)
    manifest_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    feature = {
        "clip_id": str(clip.id),
        "recording_id": str(video.id),
        "source_job_id": str(source_job.id),
        "split": "train" if fault == "non_holdout" else "holdout",
    }
    label = {
        "clip_id": str(clip.id),
        "split": "holdout",
        "label_type": "route_family" if fault == "wrong_type" else "coverage_shell",
    }
    hashes = {}
    for kind, row in [("features", feature), ("labels", label)]:
        data = (json.dumps(row) + "\n").encode()
        path = local_object_path("artifacts", f"manifest/{kind}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[f"{kind}_sha256"] = hashlib.sha256(data).hexdigest()
    features_uri, labels_uri = (
        "local://artifacts/manifest/features.jsonl",
        "local://artifacts/manifest/labels.jsonl",
    )
    dataset = TrainingDataset(
        id=dataset_id,
        model_scope="coverage",
        row_count=0,
        source_label_ids=[],
        source_correction_ids=[],
        artifact_uri=labels_uri,
        changelog={"manifest_id": str(manifest_id)},
    )
    db.add(dataset)
    await db.flush()
    db.add(
        SystemSetting(
            key=f"learning:manifest:{manifest_id}",
            updated_by=processor.id,
            value={
                "id": str(manifest_id),
                "training_dataset_id": str(dataset_id),
                "features_uri": features_uri,
                "labels_uri": labels_uri,
                "hashes": hashes,
            },
        )
    )
    job.job_type, job.video_id, job.training_dataset_id = JobType.train, None, dataset_id
    job.input_artifacts = {
        "training_variant": "local_coverage",
        "features_uri": features_uri,
        "labels_uri": labels_uri,
    }
    model.training_dataset_id = dataset_id
    model.metrics = {
        **model.metrics,
        "job_id": str(job.id),
        "features_uri": features_uri,
        "labels_uri": labels_uri,
    }
    if fault == "alternate_dataset":
        other = TrainingDataset(
            id=uuid.uuid4(),
            model_scope="coverage",
            row_count=0,
            source_label_ids=[],
            source_correction_ids=[],
            artifact_uri=labels_uri,
        )
        db.add(other)
        await db.flush()
        job.training_dataset_id = other.id
    if fault == "model_job":
        model.metrics = {**model.metrics, "job_id": str(uuid.uuid4())}
    if fault == "foreign_model":
        borrowed = ModelVersion(
            id=uuid.uuid4(),
            model_name="borrowed",
            model_type="coverage",
            version="fixture",
            artifact_uri=model.artifact_uri,
            metrics={"artifact_sha256": model.metrics["artifact_sha256"]},
            training_dataset_id=dataset_id,
        )
        db.add(borrowed)
        body["model_version_id"] = str(borrowed.id)
    if fault == "source_change":
        video.storage_uri = "local://raw-video/different.bin"
    if fault == "frozen_hash":
        local_object_path("artifacts", "manifest/features.jsonl").write_bytes(b"changed")
    if fault == "failed_source":
        source_job.status = JobStatus.failed
    await db.flush()
    previous = dict(app.dependency_overrides)

    async def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency
    app.dependency_overrides[get_current_user] = lambda: processor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/labels", json=body, headers=headers)
            if fault:
                assert response.status_code == 403, response.text
            else:
                assert response.status_code == 201, response.text
                predicted = await db.get(Label, uuid.UUID(body["id"]))
                with pytest.raises(HTTPException):
                    await require_label_proof(db, predicted, model)
                completed = await client.patch(
                    f"/api/v1/jobs/{job.id}",
                    json={
                        "status": "succeeded",
                        "worker_id": job.leased_by,
                        "lease_token": str(job.lease_token),
                        "attempt_count": job.attempt_count,
                        "output_artifacts": {"model_version_id": str(model.id)},
                    },
                )
                assert completed.status_code == 200, completed.text
                await require_label_proof(db, predicted, model)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
