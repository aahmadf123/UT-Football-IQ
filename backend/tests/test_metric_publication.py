"""Real HTTP producer-authentication contracts; no football accuracy claim."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    Clip,
    Event,
    JobStatus,
    JobType,
    Metric,
    ModelStage,
    ModelVersion,
    ProcessingJob,
    WorkerRuntime,
)
from httpx import ASGITransport, AsyncClient
from test_expected_outcome_serving import serving_fixture
from test_platform_overhaul import actor, db, player_track, recording  # noqa: F401


@pytest.mark.parametrize(
    ("fault", "expected_status"),
    [
        (None, 201),
        ("no_headers", 403),
        ("no_body_job", 403),
        ("wrong_body_job", 403),
        ("copied_fence", 403),
        ("foreign_runtime", 403),
        ("missing_runtime", 403),
        ("expired", 409),
        ("missing_expiry", 403),
        ("stale_token", 409),
        ("cancelled", 409),
        ("training_job", 403),
        ("wrong_video", 403),
        ("wrong_clip", 403),
        ("mixed_case_without_fence", 403),
        ("approved_observation_without_fence", 403),
    ],
)
async def test_borrowed_approval_never_authenticates_a_metric_producer(
    db,  # noqa: F811 -- imported rollback fixture
    monkeypatch,
    fault,
    expected_status,  # noqa: F811
):
    processor, staff = await actor(db), await actor(db)
    monkeypatch.setenv("SEED_WORKER_EMAIL", processor.email)
    get_settings.cache_clear()
    video, clip = await recording(db)
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, 1024
    video.practice_session_id = uuid.uuid4()
    # The session FK is exercised by the existing real-cohort test. This HTTP
    # fixture uses an existing actual session to keep every source reference real.
    from app.models import FootballSession

    db.add(FootballSession(id=video.practice_session_id, title="Producer protocol fixture"))
    await db.flush()
    clip.capture_regime = "fixed_sideline"
    track = await player_track(db, clip)
    mock_db, _, packet, provenance, _ = serving_fixture()
    template = mock_db.get.side_effect(ModelVersion, None)
    model = ModelVersion(
        id=uuid.uuid4(),
        model_name="protocol-only",
        version="fixture",
        model_type="expected_completion",
        promoted_stage=ModelStage.production,
        metrics=template.metrics,
    )
    worker_id = f"metric-{uuid.uuid4()}"
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by=worker_id,
        lease_token=uuid.uuid4(),
        attempt_count=2,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    runtime = WorkerRuntime(worker_id=worker_id, registered_by=processor.id)
    if fault == "foreign_runtime":
        runtime.registered_by = staff.id
    if fault != "missing_runtime":
        db.add(runtime)
    if fault == "expired":
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if fault == "missing_expiry":
        job.lease_expires_at = None
    if fault == "cancelled":
        job.control_requested = "cancel"
    if fault == "training_job":
        job.job_type = JobType.train
    if fault == "wrong_video":
        other, _ = await recording(db)
        job.video_id = other.id
    if fault == "wrong_clip":
        other_clip = Clip(id=uuid.uuid4(), video_id=video.id, start_time=6, end_time=8)
        db.add(other_clip)
        job.clip_id = other_clip.id
    db.add_all([model, job])
    await db.flush()
    packet["lineage"] = {
        "recording_id": str(video.id),
        "media_sha256": video.content_sha256,
        "session_id": str(video.practice_session_id),
        "canonical_play_id": str(clip.id),
    }
    packet["subject"]["tracklet_id"] = str(track.id)
    payload = {
        "clip_id": str(clip.id),
        "tracklet_id": str(track.id),
        "metric_name": "expected_completion",
        "metric_value": packet,
        "experimental_flag": False,
        "analytics_safe": True,
        "model_version_id": str(model.id),
        "provenance": provenance,
        "job_id": str(job.id),
    }
    headers = {
        "x-job-id": str(job.id),
        "x-worker-id": worker_id,
        "x-job-lease-token": str(job.lease_token),
    }
    if fault in {"no_headers", "mixed_case_without_fence", "approved_observation_without_fence"}:
        headers = {}
    if fault == "mixed_case_without_fence":
        payload["metric_name"] = "Expected_Completion"
    if fault == "no_body_job":
        payload.pop("job_id")
    if fault == "wrong_body_job":
        payload["job_id"] = str(uuid.uuid4())
    if fault == "stale_token":
        headers["x-job-lease-token"] = str(uuid.uuid4())
    if fault == "approved_observation_without_fence":
        db.add_all(
            [
                Event(
                    clip_id=clip.id,
                    event_type="snap",
                    timestamp_seconds=1,
                    attributes={"timebase": "recording", "shot_id": "source-shot"},
                    provenance={"source": "human"},
                ),
                Event(
                    clip_id=clip.id,
                    event_type="throw",
                    timestamp_seconds=2,
                    attributes={"timebase": "recording", "shot_id": "source-shot"},
                    provenance={"source": "human"},
                ),
            ]
        )
        await db.flush()
        payload.update(
            metric_name="time_to_throw",
            metric_value={"seconds": 1},
            provenance={
                "source": "real",
                "validated": True,
                "validation_source": "human_event_timestamps",
            },
        )

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: (
        staff if fault == "copied_fence" else processor
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/metrics", json=payload, headers=headers)
            assert response.status_code == expected_status, response.text
            if expected_status == 201:
                stored = await db.get(Metric, uuid.UUID(response.json()["id"]))
                assert stored.analytics_safe and not stored.experimental_flag
                assert stored.provenance["publication_state"] == "pending"
                assert stored.provenance["producer"] == {
                    "job_id": str(job.id),
                    "worker_id": worker_id,
                    "attempt_count": 2,
                    "principal_id": str(processor.id),
                }
                finished = await client.patch(
                    f"/api/v1/jobs/{job.id}",
                    json={
                        "status": "succeeded",
                        "worker_id": worker_id,
                        "lease_token": str(job.lease_token),
                        "attempt_count": 2,
                    },
                )
                assert finished.status_code == 200, finished.text
                assert "publication_state" not in stored.provenance
                assert stored.provenance["result_version"] == str(job.id)
    finally:
        app.dependency_overrides.clear()
