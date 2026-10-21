"""Coach reads quarantine candidates; learning/history retain original labels."""

import uuid
from unittest.mock import AsyncMock

import pytest
from app.models import (
    CaptureRegime,
    Clip,
    FieldCalibration,
    JobType,
    Label,
    ModelStage,
    ModelVersion,
    ProcessingJob,
    SystemSetting,
    Tracklet,
)
from app.services.label_visibility import coach_label_qualification, select_coach_labels
from fastapi import HTTPException
from test_clip_edits import film, workspace  # noqa: F401 -- isolated rollback fixture


def label(**changes):
    return Label(
        id=uuid.uuid4(),
        clip_id=uuid.uuid4(),
        label_type="coverage_shell",
        label_value={
            "coverage": "synthetic-contract-only",
            "provenance": {"artifact_sha256": "a" * 64},
        },
        source="model",
        model_version_id=uuid.uuid4(),
        **changes,
    )


@pytest.mark.parametrize("source", ["human", "manual"])
async def test_human_annotation_requires_actual_annotator(source):
    row = label()
    row.source = source
    db = AsyncMock()
    assert await coach_label_qualification(db, row) is None
    row.annotated_by = uuid.uuid4()
    assert await coach_label_qualification(db, row) == "human_reviewed"
    db.get.assert_not_awaited()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "candidate",
        "no_approval",
        "artifact",
        "proof",
        "wrong_type",
        "validated_false",
        "experimental",
        "old_result",
        "held_result",
        "training_result",
        "approval_missing",
        "unsupported_regime",
        "unknown_regime",
        "wrong_schema",
        "wrong_scope_type",
        "wrong_scope_artifact",
        "wrong_scope_evaluation",
        "required_evidence",
    ],
)
async def test_model_read_requires_current_approval_artifact_and_private_proof(monkeypatch, fault):
    from app.services.label_publication import classification_publication_approvals

    row = label()
    model = ModelVersion(
        id=row.model_version_id,
        model_name="Synthetic contract",
        version="1",
        model_type="coverage",
        promoted_stage=ModelStage.production,
        metrics={"approved_evaluation_id": "fixture", "approved_artifact_sha256": "a" * 64},
    )
    proof = AsyncMock()
    monkeypatch.setattr("app.services.label_publication.require_label_proof", proof)
    if fault == "candidate":
        model.promoted_stage = ModelStage.experimental
    elif fault == "no_approval":
        model.metrics = {}
    elif fault == "artifact":
        row.label_value["provenance"]["artifact_sha256"] = "b" * 64
    elif fault == "proof":
        proof.side_effect = HTTPException(422, "Missing private receipt")
    elif fault == "wrong_type":
        model.model_type = "formation"
    elif fault == "validated_false":
        row.label_value["provenance"]["validated"] = False
    elif fault == "experimental":
        row.label_value["experimental"] = True
    else:
        row.label_value["validated"] = True  # This flag alone grants nothing.
    job = ProcessingJob(id=uuid.uuid4(), job_type=JobType.pipeline, output_artifacts={})
    clip = Clip(
        id=row.clip_id,
        capture_regime=CaptureRegime.fixed_sideline,
        provenance={"published_result_version": str(job.id)},
    )
    # The exact approval document emitted by learning.promote_model; synthetic
    # protocol values do not establish an actual model evaluation or release.
    scopes = classification_publication_approvals(
        {
            "passed": True,
            "label_type": "coverage_shell",
            "feature_schema_version": 1,
            "capture_regimes": ["fixed_sideline"],
        },
        "fixture",
        "a" * 64,
    )
    scope_approval = scopes["coverage_shell"]
    if fault != "no_approval":
        model.metrics = {**model.metrics, "label_publication_approvals": scopes}
    receipt = SystemSetting(
        key=f"labels:producer:{row.id}",
        value={
            "job_id": str(job.id),
            "artifact_sha256": "a" * 64,
            "feature_schema_version": 1,
        },
    )
    if fault == "approval_missing":
        model.metrics.pop("label_publication_approvals")
    elif fault == "unsupported_regime":
        clip.capture_regime = CaptureRegime.drone_follow
    elif fault == "unknown_regime":
        clip.capture_regime = CaptureRegime.unknown
    elif fault == "wrong_schema":
        scope_approval["feature_schema_version"] = 2
    elif fault == "wrong_scope_type":
        model.metrics["label_publication_approvals"] = {"route_family": scope_approval}
    elif fault == "wrong_scope_artifact":
        scope_approval["artifact_sha256"] = "b" * 64
    elif fault == "wrong_scope_evaluation":
        scope_approval["evaluation_id"] = "other"
    elif fault == "required_evidence":
        scope_approval["required_evidence"] = "caller-validated"
    if fault == "old_result":
        clip.provenance = {"published_result_version": str(uuid.uuid4())}
    elif fault == "held_result":
        job.output_artifacts = {"publication_holds": {str(clip.id): "replacement_not_qualified"}}
    elif fault == "training_result":
        job.job_type = JobType.train
    db = AsyncMock()
    db.get.side_effect = lambda cls, _id: {
        ModelVersion: model,
        Clip: clip,
        ProcessingJob: job,
        SystemSetting: receipt,
    }[cls]
    assert await coach_label_qualification(db, row) == (None if fault else "model_approved")
    if not fault:
        proof.assert_awaited_once_with(db, row, model)


async def test_identical_human_copies_override_model_and_conflicts_require_review():
    db = AsyncMock()
    first, second, candidate = label(), label(), label()
    for row in (first, second):
        row.source, row.annotated_by = "human", uuid.uuid4()
    selected = await select_coach_labels(db, [first, second, candidate])
    assert len(selected.labels) == 1 and selected.labels[0][1] == "human_reviewed"
    assert selected.conflicting_types == []
    db.get.assert_not_awaited()
    second.label_value = {"coverage": "different-explicit-review"}
    selected = await select_coach_labels(db, [first, second, candidate])
    assert selected.labels == [] and selected.conflicting_types == ["coverage_shell"]


async def test_real_overlay_hides_unapproved_labels_but_history_keeps_them(workspace):  # noqa: F811
    client, db, session = workspace
    _, (clip,) = await film(db, session, [(0, 1)])
    response = await client.post(
        "/api/v1/corrections",
        json={
            "clip_id": str(clip.id),
            "correction_type": "coverage_tag",
            "corrected_value": {"coverage": "synthetic-reviewed-contract"},
        },
    )
    assert response.status_code == 201, response.text
    candidate = label()
    candidate.clip_id = clip.id
    candidate.model_version_id = None
    candidate.label_value["provenance"]["validated"] = False
    forged = label()
    forged.clip_id, forged.model_version_id = clip.id, None
    forged.label_value["validated"] = True
    forged.label_value["experimental"] = False
    db.add_all([candidate, forged])
    await db.flush()
    history = (await client.get(f"/api/v1/clips/{clip.id}/labels")).json()
    assert len(history) == 3
    overlay = (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()
    assert len(overlay["labels"]) == 1
    assert overlay["labels"][0]["qualification"] == "human_reviewed"
    assert overlay["labels"][0]["label_value"]["coverage"] == "synthetic-reviewed-contract"
    assert overlay["withheld_label_count"] == 2
    assert any("unapproved" in warning for warning in overlay["warnings"])
    assert len((await client.get(f"/api/v1/clips/{clip.id}/labels")).json()) == 3
    # The real correction writer rewrites all matching model/history rows.
    # Effective coach reads deduplicate those human copies, not their history.
    corrected = await client.post(
        "/api/v1/corrections",
        json={
            "clip_id": str(clip.id),
            "correction_type": "coverage_tag",
            "corrected_value": {"coverage": "synthetic-corrected-contract"},
        },
    )
    assert corrected.status_code == 201, corrected.text
    all_rows = (await client.get(f"/api/v1/clips/{clip.id}/labels")).json()
    assert len(all_rows) == 3 and all(row["source"] == "human" for row in all_rows)
    after = (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()
    assert len(after["labels"]) == 1
    assert after["labels"][0]["label_value"]["coverage"] == "synthetic-corrected-contract"
    conflicting = label()
    conflicting.clip_id, conflicting.model_version_id = clip.id, None
    conflicting.source = "human"
    conflicting.annotated_by = uuid.UUID(all_rows[0]["annotated_by"])
    conflicting.label_value = {"coverage": "synthetic-distinct-review"}
    db.add(conflicting)
    await db.flush()
    conflicted = (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()
    assert conflicted["labels"] == []
    assert any("Conflicting staff annotations" in warning for warning in conflicted["warnings"])


async def test_actual_producer_completion_and_publication_select_current_coach_label(
    workspace,  # noqa: F811 -- isolated rollback fixture
    monkeypatch,
    tmp_path,
):
    """Real HTTP proof plus publisher, with explicit synthetic approval fixture."""
    from app.services.label_publication import classification_publication_approvals
    from app.services.publication import publish_job_clip_tiers
    from label_producer_helpers import publish_pipeline_predictions
    from test_label_publication import fixture as producer_fixture

    client, db, _session = workspace
    processor, _staff, video, clip, model, _job, _body, _headers = await producer_fixture(
        db, monkeypatch, tmp_path
    )
    clip.capture_regime = video.capture_regime = CaptureRegime.fixed_sideline
    first = Label(
        id=uuid.uuid4(),
        clip_id=clip.id,
        label_type="coverage_shell",
        label_value={"coverage": "synthetic-first"},
        source="model",
        model_version_id=model.id,
    )
    job = await publish_pipeline_predictions(db, processor, video, model, [first], monkeypatch)
    model.promoted_stage = ModelStage.production
    model.metrics = {
        **model.metrics,
        "approved_evaluation_id": "synthetic-protocol-approval",
        "approved_artifact_sha256": model.metrics["artifact_sha256"],
        "label_publication_approvals": classification_publication_approvals(
            {
                "passed": True,
                "label_type": "coverage_shell",
                "feature_schema_version": 1,
                "capture_regimes": ["fixed_sideline"],
            },
            "synthetic-protocol-approval",
            model.metrics["artifact_sha256"],
        ),
    }
    track = Tracklet(
        id=uuid.uuid4(),
        clip_id=clip.id,
        job_id=job.id,
        start_frame=0,
        end_frame=1,
        provenance={"source": "real", "result_version": str(job.id)},
    )
    db.add(track)
    await db.flush()
    await publish_job_clip_tiers(db, job)
    assert clip.provenance["published_result_version"] == str(job.id)
    assert job.output_artifacts["publication_holds"] == {}
    await db.flush()
    displayed = (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()
    assert [row["id"] for row in displayed["labels"]] == [str(first.id)]
    assert displayed["labels"][0]["qualification"] == "model_approved"

    second = Label(
        id=uuid.uuid4(),
        clip_id=clip.id,
        label_type="coverage_shell",
        label_value={"coverage": "synthetic-replacement"},
        source="model",
        model_version_id=model.id,
    )
    newer = await publish_pipeline_predictions(db, processor, video, model, [second], monkeypatch)
    replacement = Tracklet(
        id=uuid.uuid4(),
        clip_id=clip.id,
        job_id=newer.id,
        start_frame=0,
        end_frame=1,
        provenance={
            "source": "real",
            "result_version": str(newer.id),
            "exclusion_reason": "replacement_not_qualified",
        },
    )
    db.add(replacement)
    await db.flush()
    # Real publisher retains the prior selected version when the new run is held.
    await publish_job_clip_tiers(db, newer)
    assert clip.provenance["published_result_version"] == str(job.id)
    held = (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()
    assert [row["id"] for row in held["labels"]] == [str(first.id)]
    replacement.provenance = {"source": "real", "result_version": str(newer.id)}
    await db.flush()
    await publish_job_clip_tiers(db, newer)
    current = (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()
    assert [row["id"] for row in current["labels"]] == [str(second.id)]
    assert len((await client.get(f"/api/v1/clips/{clip.id}/labels")).json()) == 2
    # Current source-regime changes invalidate both private provenance and
    # supported release scope; retained raw history never grants approval.
    clip.capture_regime = CaptureRegime.drone_follow
    await db.flush()
    assert (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()["labels"] == []
    clip.capture_regime, video.capture_regime = (
        CaptureRegime.fixed_sideline,
        CaptureRegime.unconstrained,
    )
    await db.flush()
    assert (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()["labels"] == []
    assert len((await client.get(f"/api/v1/clips/{clip.id}/labels")).json()) == 2
    video.capture_regime = CaptureRegime.fixed_sideline
    calibration = await db.get(FieldCalibration, clip.calibration_version_id)
    assert calibration is not None
    calibration.analytics_safe = False
    await db.flush()
    assert (await client.get(f"/api/v1/clips/{clip.id}/overlays")).json()["labels"] == []
