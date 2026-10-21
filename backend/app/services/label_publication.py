"""Private producer attestations for actual generic model-label execution."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Clip,
    FieldCalibration,
    JobStatus,
    JobType,
    Label,
    ModelVersion,
    ProcessingJob,
    SystemSetting,
    TrainingDataset,
    User,
    Video,
    WorkerRuntime,
)
from app.services.annotations import annotation_revision
from app.services.clip_state import clip_is_active
from app.services.expected_outcomes import _hash, _read, trusted_worker
from app.services.media_identity import verified_job_media_identity, verified_media_sha256

PREFIX = "labels:producer:"
KNOWN_REGIMES = frozenset({"fixed_sideline", "fixed_endzone", "drone_follow", "unconstrained"})


def artifact_feature_schema(model: ModelVersion) -> int:
    document = _read(str(model.artifact_uri))
    schema = document.get("schema_version") if isinstance(document, dict) else None
    if type(schema) is not int or schema != 1:
        raise ValueError("Classifier artifact has no explicit supported feature schema identity")
    return schema


def classification_publication_approvals(
    scope: dict[str, Any], evaluation_id: str, artifact_sha256: str
) -> dict[str, Any]:
    """The exact scope retained after recomputed classification gates pass."""
    if scope.get("passed") is not True:
        raise ValueError("Classification capture scope did not pass")
    return {
        scope["label_type"]: {
            "policy_version": 1,
            "artifact_sha256": artifact_sha256,
            "evaluation_id": evaluation_id,
            "feature_schema_version": scope["feature_schema_version"],
            "capture_regimes": scope["capture_regimes"],
            "required_evidence": "trusted_label_processor_v1",
        }
    }


def label_scope_approved(
    model: ModelVersion, label: Label, clip: Clip, proof: dict[str, Any]
) -> bool:
    from app.services.learning import canonical_label_type

    kind = canonical_label_type(label.label_type)
    metadata = model.metrics or {}
    if not isinstance(metadata, dict) or not isinstance(proof, dict):
        return False
    approvals = metadata.get("label_publication_approvals")
    approval = approvals.get(kind) if isinstance(approvals, dict) else None
    regime = str(getattr(clip.capture_regime, "value", clip.capture_regime))
    return bool(
        isinstance(approval, dict)
        and type(approval.get("policy_version")) is int
        and approval["policy_version"] == 1
        and approval.get("artifact_sha256")
        == metadata.get("approved_artifact_sha256")
        == proof.get("artifact_sha256")
        and approval.get("evaluation_id") == metadata.get("approved_evaluation_id")
        and isinstance(approval.get("evaluation_id"), str)
        and bool(approval["evaluation_id"])
        and type(approval.get("feature_schema_version")) is int
        and approval.get("feature_schema_version") == proof.get("feature_schema_version")
        and type(proof.get("feature_schema_version")) is int
        and approval.get("required_evidence") == "trusted_label_processor_v1"
        and isinstance(approval.get("capture_regimes"), list)
        and bool(approval["capture_regimes"])
        and all(
            isinstance(value, str) and value in KNOWN_REGIMES
            for value in approval["capture_regimes"]
        )
        and regime in KNOWN_REGIMES
        and regime in approval["capture_regimes"]
    )


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def label_identity(label: Label) -> dict[str, Any]:
    return {
        "id": str(label.id),
        "clip_id": str(label.clip_id),
        "tracklet_id": str(label.tracklet_id) if label.tracklet_id else None,
        "label_type": label.label_type,
        "label_value": label.label_value,
        "source": label.source,
        "model_version_id": str(label.model_version_id),
        "dataset_version": label.dataset_version,
    }


def _rows(uri: str, expected: str) -> list[dict[str, Any]]:
    # Generic learning is currently a local-library workflow, matching its
    # frozen label snapshot and model promotion adapters.
    from app.storage import ARTIFACTS, local_object_path, parse_storage_uri

    scheme, bucket, key = parse_storage_uri(uri)
    if scheme != "local" or bucket != ARTIFACTS:
        raise ValueError("Frozen classification features must exist in the local library")
    path = local_object_path(bucket, key)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Frozen classification features exceed the size limit")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != _hash(expected):
        raise ValueError("Frozen classification features changed")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


async def _training_scope(
    db: AsyncSession, job: ProcessingJob, model: ModelVersion, clip: Clip, label_type: str
) -> dict[str, Any]:
    from app.services.learning import canonical_label_type
    from app.services.learning_manifests import _observations_current

    inputs, metrics = job.input_artifacts or {}, model.metrics or {}
    dataset = (
        await db.get(TrainingDataset, job.training_dataset_id) if job.training_dataset_id else None
    )
    data = (dataset.changelog or {}) if dataset else {}
    manifest = await db.get(SystemSetting, f"learning:manifest:{data.get('manifest_id')}")
    frozen = manifest.value if manifest else {}
    if (
        inputs.get("training_variant") != "local_coverage"
        or dataset is None
        or model.training_dataset_id != dataset.id
        or metrics.get("job_id") != str(job.id)
        or frozen.get("training_dataset_id") != str(dataset.id)
        or inputs.get("features_uri") != frozen.get("features_uri")
        or inputs.get("labels_uri") != frozen.get("labels_uri")
        or metrics.get("features_uri") != frozen.get("features_uri")
        or metrics.get("labels_uri") != frozen.get("labels_uri")
        or model.model_type != "coverage"
        or canonical_label_type(label_type) != "coverage_shell"
    ):
        raise ValueError("Candidate label is not bound to this exact frozen training job/manifest")
    hashes = frozen["hashes"]
    rows = await to_thread.run_sync(_rows, frozen["features_uri"], hashes["features_sha256"])
    labels = await to_thread.run_sync(_rows, frozen["labels_uri"], hashes["labels_sha256"])
    targets = [
        row for row in rows if row.get("clip_id") == str(clip.id) and row.get("split") == "holdout"
    ]
    truth = [
        row
        for row in labels
        if row.get("clip_id") == str(clip.id)
        and row.get("split") == "holdout"
        and canonical_label_type(row.get("label_type", "")) == canonical_label_type(label_type)
    ]
    if len(targets) != 1 or len(truth) != 1 or targets[0].get("recording_id") != str(clip.video_id):
        raise ValueError(
            "Candidate prediction is outside the actual frozen holdout membership/type"
        )
    if not _observations_current(targets[0], clip):
        raise ValueError("Frozen holdout observations are no longer current")
    source_job = await db.get(ProcessingJob, uuid.UUID(targets[0]["source_job_id"]))
    if (
        source_job is None
        or source_job.video_id != clip.video_id
        or await verified_job_media_identity(db, source_job) is None
    ):
        raise ValueError("Frozen holdout source lacks a trusted completed source processor")
    return {
        "manifest_id": frozen["id"],
        "features_sha256": hashes["features_sha256"],
        "labels_sha256": hashes["labels_sha256"],
        "feature_row_sha256": digest(targets[0]),
        "source_job_id": str(source_job.id),
        "source_attempt": source_job.attempt_count,
    }


async def _source(db: AsyncSession, clip: Clip, *, geometry: bool = False) -> dict[str, Any]:
    video = await db.get(Video, clip.video_id)
    full = await verified_media_sha256(db, video) if video else None
    if video is None or not full or not clip_is_active(clip):
        raise ValueError("Label source lacks current verified media identity")
    calibration = None
    if geometry:
        row = (
            await db.get(FieldCalibration, clip.calibration_version_id)
            if clip.calibration_version_id
            else None
        )

        def number(value: Any) -> bool:
            return type(value) in (int, float) and math.isfinite(value)

        if (
            row is None
            or row.video_id != clip.video_id
            or row.analytics_safe is not True
            or not number(row.confidence)
            or not number(row.confidence_threshold)
            or row.confidence is None
            or not 0.7 <= row.confidence_threshold <= row.confidence <= 1
            or not isinstance(row.homography, list)
            or len(row.homography) != 9
            or not all(number(value) for value in row.homography)
            or row.reason_codes not in (None, [])
        ):
            raise ValueError("Coverage label requires its actual current safe field calibration")
        matrix = row.homography
        determinant = (
            matrix[0] * (matrix[4] * matrix[8] - matrix[5] * matrix[7])
            - matrix[1] * (matrix[3] * matrix[8] - matrix[5] * matrix[6])
            + matrix[2] * (matrix[3] * matrix[7] - matrix[4] * matrix[6])
        )
        if not math.isfinite(determinant) or abs(determinant) < 1e-12:
            raise ValueError("Coverage field calibration is singular")
        calibration = {
            "id": str(row.id),
            "video_id": str(row.video_id),
            "homography": row.homography,
            "confidence": row.confidence,
            "confidence_threshold": row.confidence_threshold,
            "analytics_safe": row.analytics_safe,
            "reason_codes": row.reason_codes,
            "provenance": row.provenance,
            "job_id": str(row.job_id),
            "model_version_id": str(row.model_version_id),
        }
    return {
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "registration_fingerprint": video.content_sha256,
        "size_bytes": video.size_bytes,
        "full_sha256": full,
        "session_id": str(video.practice_session_id),
        "clip_id": str(clip.id),
        "start_time": clip.start_time,
        "end_time": clip.end_time,
        "annotation_revision": annotation_revision(clip),
        "calibration_version_id": str(clip.calibration_version_id),
        "field_calibration": calibration,
        "capture_regime": str(getattr(clip.capture_regime, "value", clip.capture_regime)),
        "video_capture_regime": str(getattr(video.capture_regime, "value", video.capture_regime)),
        "observation_revision": (clip.label_data or {}).get("_observation_revision", 0),
    }


async def prepare_label_proof(
    db: AsyncSession, label: Label, job: ProcessingJob | None, actor: User
) -> dict[str, Any]:
    try:
        if job is None or (label.label_value.get("provenance") or {}).get("job_id") != str(job.id):
            raise ValueError("Real model labels require the trusted current processor job")
        clip = await db.get(Clip, label.clip_id)
        model = (
            await db.get(ModelVersion, label.model_version_id) if label.model_version_id else None
        )
        if clip is None or model is None or not model.artifact_uri:
            raise ValueError("Label requires its actual clip and registered model artifact")
        if job.job_type == JobType.train and label.tracklet_id is not None:
            raise ValueError("Frozen local coverage predictions have clip-level target scope")
        provenance = label.label_value.get("provenance") or {}
        if label.label_value.get("source", "real") != "real":
            raise ValueError("Contradictory synthetic or heuristic label source")
        actual = await to_thread.run_sync(lambda: _read(str(model.artifact_uri), parse=False))
        feature_schema = await to_thread.run_sync(artifact_feature_schema, model)
        if provenance.get("feature_schema_version", feature_schema) != feature_schema:
            raise ValueError("Label feature schema differs from actual classifier artifact")
        if (
            _hash(provenance.get("artifact_sha256")) != actual
            or (model.metrics or {}).get("artifact_sha256") != actual
        ):
            raise ValueError("Label model artifact differs from actual registered bytes")
        from app.services.learning import canonical_label_type

        if {
            "coverage_shell": "coverage",
            "route_family": "route",
            "offensive_formation": "formation",
        }.get(canonical_label_type(label.label_type)) != model.model_type:
            raise ValueError("Label type differs from its registered model task")
        scope = {}
        if job.job_type == JobType.pipeline:
            if job.video_id != clip.video_id or (
                job.clip_id is not None and job.clip_id != clip.id
            ):
                raise ValueError("Label target differs from its processor clip/video scope")
        elif job.job_type == JobType.train:
            scope = await _training_scope(db, job, model, clip, label.label_type)
        else:
            raise ValueError("Unsupported model label producer job")
        return {
            "schema_version": 1,
            "label_sha256": digest(label_identity(label)),
            "job_id": str(job.id),
            "attempt_count": job.attempt_count,
            "worker_id": job.leased_by,
            "principal_id": str(actor.id),
            "lease_sha256": hashlib.sha256(str(job.lease_token).encode()).hexdigest(),
            "model_id": str(model.id),
            "artifact_uri": model.artifact_uri,
            "artifact_sha256": actual,
            "feature_schema_version": feature_schema,
            "source": await _source(db, clip, geometry=model.model_type == "coverage"),
            "training_scope": scope,
        }
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise HTTPException(403, f"Model label producer refused: {exc}") from exc


async def store_label_proof(
    db: AsyncSession, label: Label, proof: dict[str, Any], actor: User
) -> None:
    key = f"{PREFIX}{label.id}"
    prior = await db.get(SystemSetting, key)
    if prior is not None:
        if prior.value != proof or prior.updated_by != actor.id:
            raise HTTPException(409, "Label producer proof is immutable; publish a new label ID")
        return
    db.add(SystemSetting(key=key, value=proof, updated_by=actor.id))
    pending = f"labels:producer-job:{proof['job_id']}:{proof['attempt_count']}"
    if await db.get(SystemSetting, pending) is None:
        db.add(
            SystemSetting(
                key=pending,
                updated_by=actor.id,
                value={
                    k: proof[k]
                    for k in (
                        "job_id",
                        "attempt_count",
                        "worker_id",
                        "principal_id",
                        "lease_sha256",
                    )
                },
            )
        )
    await db.flush()


async def bind_label_completion(
    db: AsyncSession,
    job: ProcessingJob,
    actor: User,
    output: dict[str, Any],
    *,
    worker_id: str | None,
    lease_token: uuid.UUID | None,
    attempt_count: int | None,
) -> None:
    pending = await db.get(SystemSetting, f"labels:producer-job:{job.id}:{job.attempt_count}")
    if not isinstance(pending, SystemSetting):
        return
    runtime = await db.get(WorkerRuntime, worker_id) if worker_id else None
    if (
        not trusted_worker(actor, runtime)
        or job.status != JobStatus.running
        or job.leased_by != worker_id
        or job.lease_token != lease_token
        or job.attempt_count != attempt_count
        or not job.lease_expires_at
        or job.lease_expires_at <= datetime.now(UTC)
        or pending.updated_by != actor.id
        or pending.value.get("lease_sha256")
        != hashlib.sha256(str(lease_token).encode()).hexdigest()
    ):
        raise HTTPException(
            403, "Model label completion requires the trusted current processor lease"
        )
    key = f"labels:completed:{job.id}:{job.attempt_count}"
    receipt = {**pending.value, "worker_output_sha256": digest(output), "output_sha256": None}
    prior = await db.get(SystemSetting, key)
    if prior is not None and prior.value != receipt:
        raise HTTPException(409, "Model label completion receipt is immutable")
    if prior is None:
        db.add(SystemSetting(key=key, updated_by=actor.id, value=receipt))
    await db.flush()


async def finalize_label_completion(db: AsyncSession, job: ProcessingJob) -> None:
    """Freeze the final server publication document in the same request transaction."""
    receipt = await db.get(SystemSetting, f"labels:completed:{job.id}:{job.attempt_count}")
    if isinstance(receipt, SystemSetting):
        final = digest(job.output_artifacts or {})
        if receipt.value.get("output_sha256") not in {None, final}:
            raise HTTPException(409, "Final label producer completion is immutable")
        receipt.value = {**receipt.value, "output_sha256": final}
        await db.flush()


async def require_label_proof(db: AsyncSession, label: Label, model: ModelVersion) -> None:
    try:
        row = await db.get(SystemSetting, f"{PREFIX}{label.id}")
        proof = row.value if row and row.updated_by else {}
        if type(proof.get("schema_version")) is not int or proof.get("schema_version") != 1:
            raise ValueError("Unknown private model label proof schema")
        job = await db.get(ProcessingJob, uuid.UUID(proof["job_id"]))
        actor = await db.get(User, uuid.UUID(proof["principal_id"]))
        runtime = await db.get(WorkerRuntime, proof["worker_id"])
        receipt = (
            await db.get(SystemSetting, f"labels:completed:{job.id}:{job.attempt_count}")
            if job
            else None
        )
        clip = await db.get(Clip, label.clip_id)
        if (
            row is None
            or job is None
            or actor is None
            or clip is None
            or job.status != JobStatus.succeeded
            or job.attempt_count != proof["attempt_count"]
            or row.updated_by != actor.id
            or not trusted_worker(actor, runtime)
            or receipt is None
            or receipt.updated_by != actor.id
            or any(
                receipt.value.get(k) != proof[k]
                for k in ("job_id", "attempt_count", "worker_id", "principal_id", "lease_sha256")
            )
        ):
            raise ValueError("Model label completed attempt or trusted principal changed")
        if receipt.value.get("output_sha256") != digest(job.output_artifacts or {}):
            raise ValueError("Model label completion output changed")
        if proof["label_sha256"] != digest(label_identity(label)):
            raise ValueError("Model label immutable body changed")
        if (
            proof["model_id"] != str(model.id)
            or proof["artifact_uri"] != model.artifact_uri
            or proof["artifact_sha256"]
            != await to_thread.run_sync(lambda: _read(str(model.artifact_uri), parse=False))
        ):
            raise ValueError("Model label actual artifact changed")
        if proof["source"] != await _source(db, clip, geometry=model.model_type == "coverage"):
            raise ValueError("Model label source changed")
        if job.job_type == JobType.pipeline:
            if (
                job.video_id != clip.video_id
                or (job.clip_id is not None and job.clip_id != clip.id)
                or await verified_job_media_identity(db, job) is None
            ):
                raise ValueError("Label pipeline source completion is unverified")
        elif job.job_type == JobType.train:
            if proof["training_scope"] != await _training_scope(
                db, job, model, clip, label.label_type
            ):
                raise ValueError("Frozen candidate label membership changed")
        else:
            raise ValueError("Unsupported label producer")
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(
            422, f"Model label lacks current trusted producer proof: {exc}"
        ) from exc
