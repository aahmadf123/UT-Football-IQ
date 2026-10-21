"""Protocol fixtures exercise prediction attribution, never football accuracy."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.models import ModelStage, ModelVersion, Video
from app.services.expected_outcomes import FEATURE_VERSION, POPULATIONS
from app.services.metric_definitions import DEFINITIONS
from app.services.metric_eligibility import automatic_exclusion


def serving_fixture():
    clip = SimpleNamespace(
        id=uuid.uuid4(),
        video_id=uuid.uuid4(),
        start_time=1.0,
        end_time=3.0,
        capture_regime="fixed_sideline",
    )
    video = SimpleNamespace(
        id=clip.video_id,
        content_sha256="a" * 64,
        practice_session_id=uuid.uuid4(),
        size_bytes=1024,
        storage_uri="local://raw-video/fixture.mp4",
    )
    canonical, actor = uuid.uuid4(), uuid.uuid4()
    definition = DEFINITIONS["expected_completion"]
    model = SimpleNamespace(
        model_type="expected_completion",
        promoted_stage=ModelStage.production,
        metrics={
            "approved_evaluation_id": "approved",
            "approved_artifact_sha256": "b" * 64,
            "publication_approvals": {
                "expected_completion": {
                    "feature_version": FEATURE_VERSION,
                    "capture_regimes": ["fixed_sideline"],
                    "definition_version": definition["version"],
                }
            },
        },
    )
    value = {
        "task": "completion",
        "feature_version": FEATURE_VERSION,
        "weights_sha256": "b" * 64,
        "eligible_population": POPULATIONS["completion"],
        "value": 0.6,
        "validated": True,
        "experimental": False,
        "analytics_safe": True,
        "capture_regime": "fixed_sideline",
        "feature_evidence_sha256": ["c" * 64],
        "observation_clock": {"event": "pass_release", "timestamp_seconds": 2.0},
        "lineage": {
            "recording_id": str(video.id),
            "media_sha256": video.content_sha256,
            "session_id": str(video.practice_session_id),
            "canonical_play_id": str(canonical),
        },
        "subject": {"scope": "player", "role": "QB", "tracklet_id": str(actor)},
    }
    provenance = {
        "source": "real",
        "validated": True,
        "artifact_sha256": "b" * 64,
        "approved_evaluation_id": "approved",
        "definition_version": definition["version"],
        "quality_checks_passed": True,
        "evidence": {key: True for key in definition["required_evidence"]},
    }
    db = AsyncMock()
    db.get.side_effect = lambda cls, key: (
        model
        if cls is ModelVersion
        else video
        if cls is Video
        else SimpleNamespace(id=actor, clip_id=clip.id)
    )
    db.execute.return_value.scalar_one_or_none = lambda: canonical
    return db, clip, value, provenance, actor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [None, "recording_id", "media_sha256", "session_id", "canonical_play_id", "clock", "actor"],
)
async def test_approved_prediction_stays_bound_to_actual_clip_view_and_actor(fault):
    db, clip, value, provenance, actor = serving_fixture()
    if fault in value["lineage"]:
        value["lineage"][fault] = "foreign"
    elif fault == "clock":
        value["observation_clock"]["timestamp_seconds"] = 3.1
    elif fault == "actor":
        actor = uuid.uuid4()
    result = await automatic_exclusion(
        db,
        clip=clip,
        name="expected_completion",
        value=copy.deepcopy(value),
        provenance=provenance,
        model_id=uuid.uuid4(),
        tracklet_id=actor,
    )
    assert (result is None) is (fault is None)


@pytest.mark.asyncio
async def test_team_prediction_cannot_impersonate_player_measurement():
    db, clip, value, provenance, actor = serving_fixture()
    value["subject"] = {"scope": "team", "reason": "actor_not_attributed"}
    assert await automatic_exclusion(
        db,
        clip=clip,
        name="expected_completion",
        value=value,
        provenance=provenance,
        model_id=uuid.uuid4(),
        tracklet_id=actor,
    )
    assert await automatic_exclusion(
        db,
        clip=clip,
        name="expected_completion",
        value=value,
        provenance=provenance,
        model_id=uuid.uuid4(),
        tracklet_id=None,
    )


@pytest.mark.asyncio
async def test_large_recording_prediction_uses_protected_full_identity():
    from app.models import JobStatus, JobType, ProcessingJob, SystemSetting
    from app.storage import FULL_HASH_BYTES

    db, clip, value, provenance, actor = serving_fixture()
    get = db.get.side_effect
    video = get(Video, clip.video_id)
    video.size_bytes = FULL_HASH_BYTES + 1
    receipt = {
        "schema_version": 1,
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "size_bytes": video.size_bytes,
        "registration_fingerprint": video.content_sha256,
        "registration_algorithm": "sampled-sha256-v1",
        "full_sha256": "d" * 64,
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.succeeded,
        attempt_count=1,
        output_artifacts={"source_identity": receipt},
    )
    proof = SimpleNamespace(
        updated_by=uuid.uuid4(),
        value={
            "source_identity": receipt,
            "job_id": str(job.id),
            "attempt_count": 1,
            "lease_sha256": "e" * 64,
            "worker_id": "trusted",
        },
    )
    db.get.side_effect = lambda cls, key: (
        proof if cls is SystemSetting else job if cls is ProcessingJob else get(cls, key)
    )
    value["lineage"]["media_sha256"] = receipt["full_sha256"]
    assert (
        await automatic_exclusion(
            db,
            clip=clip,
            name="expected_completion",
            value=value,
            provenance=provenance,
            model_id=uuid.uuid4(),
            tracklet_id=actor,
        )
        is None
    )
    video.storage_uri = "local://raw-video/replaced.mp4"
    assert await automatic_exclusion(
        db,
        clip=clip,
        name="expected_completion",
        value=value,
        provenance=provenance,
        model_id=uuid.uuid4(),
        tracklet_id=actor,
    )
