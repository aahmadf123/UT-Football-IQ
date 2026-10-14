"""Measured learning from persisted human holdouts and real candidate predictions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, Label, ModelStage, ModelVersion, SystemSetting, TrainingDataset, User
from app.services.annotations import TAG_TYPES, annotation_revision
from app.storage import ARTIFACTS, local_object_path, parse_storage_uri

BENCHMARK_PREFIX = "learning:benchmark:"
EVALUATION_PREFIX = "learning:evaluation:"
MIN_SAMPLES = 20
MIN_ACCURACY = 0.9
MIN_MACRO_F1 = 0.85


def _artifact_info(uri: str | None) -> tuple[str, set[uuid.UUID] | None]:
    """Bind release evidence to the actual local checkpoint, not metrics claims."""
    try:
        scheme, bucket, key = parse_storage_uri(uri or "")
        if scheme != "local" or bucket != ARTIFACTS:
            raise ValueError("Model artifact must be imported into this local library")
        path = local_object_path(bucket, key)
        if not path.is_file():
            raise ValueError("Registered model artifact is unavailable")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        used_clips = None
        if path.suffix == ".json" and path.stat().st_size < 16 * 1024 * 1024:
            checkpoint = json.loads(path.read_text())
            provenance = checkpoint.get("provenance", {}) if isinstance(checkpoint, dict) else {}
            if isinstance(provenance, dict) and "train_clip_ids" in provenance:
                # Calibration is model fitting too; release holdouts may never
                # be used to choose temperature or any other model parameter.
                used_clips = {
                    uuid.UUID(raw)
                    for raw in [
                        *provenance["train_clip_ids"],
                        *provenance.get("calibration_clip_ids", []),
                    ]
                }
        return digest.hexdigest(), used_clips
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _training_truths(dataset: TrainingDataset) -> tuple[list[str], str]:
    """Use the frozen fitted-label snapshot, not subsequently edited live labels."""
    try:
        scheme, bucket, key = parse_storage_uri(dataset.artifact_uri)
        if scheme != "local" or bucket != ARTIFACTS:
            raise ValueError("Training snapshot must be available in this local library")
        path = local_object_path(bucket, key)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Training snapshot exceeds the evaluation size limit")
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        changelog = dataset.changelog or {}
        expected = changelog.get("sha256") or changelog.get("hashes", {}).get(
            "training_labels_sha256"
        )
        if expected and digest != expected:
            raise ValueError("Training snapshot changed after materialization")
        classes = []
        for line in body.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if (
                not isinstance(record, dict)
                or record.get("source") != "human"
                or record.get("split", "train") != "train"
                or not isinstance(record.get("label_value"), dict)
                or not isinstance(record.get("label_type"), str)
            ):
                raise ValueError("Training snapshot needs human training truth records")
            classes.append(normalized_label(record["label_value"], record["label_type"]))
        if len(classes) != dataset.row_count or not classes:
            raise ValueError("Training snapshot row count differs from its registered dataset")
        return classes, digest
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def canonical_label_type(label_type: str) -> str:
    return next(
        (value for kind, value in TAG_TYPES.items() if kind.value == label_type), label_type
    )


def normalized_label(value: dict[str, Any], label_type: str | None = None) -> str:
    import json

    cleaned = {
        k: v
        for k, v in value.items()
        if not k.startswith("_")
        and k
        not in {
            "confidence",
            "source",
            "provenance",
            "experimental",
            "review_required",
        }
    }
    for aliases, canonical in (
        (("shell", "coverage_shell"), "coverage"),
        (("route_family",), "route"),
        (("personnel",), "personnel_grouping"),
    ):
        for alias in aliases:
            if alias in cleaned:
                cleaned.setdefault(canonical, cleaned.pop(alias))
    for semantic_key in ("coverage", "formation", "route", "personnel_grouping"):
        if semantic_key in cleaned:
            cleaned = {semantic_key: cleaned[semantic_key]}
            break
    expected_key = {
        "coverage_shell": "coverage",
        "offensive_formation": "formation",
        "route_family": "route",
        "personnel_grouping": "personnel_grouping",
    }.get(canonical_label_type(label_type or ""))
    if expected_key:
        raw = next(
            (
                cleaned[key]
                for key in (expected_key, "value", "label", "class", "name")
                if key in cleaned
            ),
            None,
        )
        if isinstance(raw, str):
            cleaned = {expected_key: "_".join(raw.strip().lower().replace("-", " ").split())}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))


async def create_benchmark(
    db: AsyncSession, *, label_ids: list[uuid.UUID], actor: User, name: str
) -> dict[str, Any]:
    ids = list(dict.fromkeys(label_ids))
    labels = list(
        (await db.execute(select(Label).where(Label.id.in_(ids)).with_for_update())).scalars().all()
    )
    if len(labels) != len(ids) or not labels:
        raise HTTPException(422, detail="Every benchmark label must exist")
    if any(label.source != "human" or label.clip_id is None for label in labels):
        raise HTTPException(
            422, detail="Benchmark truth must be human labels attached to real film"
        )
    targets = {
        (label.clip_id, label.tracklet_id, canonical_label_type(label.label_type))
        for label in labels
    }
    if len(targets) != len(labels):
        raise HTTPException(422, detail="Benchmark targets must each have one human truth label")
    clip_ids = {label.clip_id for label in labels}
    clips = list(
        (await db.execute(select(Clip).where(Clip.id.in_(clip_ids)).with_for_update()))
        .scalars()
        .all()
    )
    for clip in clips:
        if not clip.is_reviewed:
            raise HTTPException(422, detail="Complete clip review before locking benchmark truth")
        data = clip.label_data if isinstance(clip.label_data, dict) else {}
        provenance = getattr(clip, "provenance", None) or data.get("provenance", {})
        if isinstance(provenance, dict) and provenance.get("source") in {
            "stub",
            "synthetic",
            "synthetic_test",
        }:
            raise HTTPException(422, detail="Synthetic film cannot be a release benchmark")
        clip.label_data = {**data, "_learning_split": "holdout"}
    benchmark_id = uuid.uuid4()
    revisions = {clip.id: annotation_revision(clip) for clip in clips}
    value = {
        "id": str(benchmark_id),
        "name": name,
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": str(actor.id),
        "samples": [
            {
                "truth_label_id": str(label.id),
                "clip_id": str(label.clip_id),
                "tracklet_id": str(label.tracklet_id) if label.tracklet_id else None,
                "label_type": label.label_type,
                "truth": label.label_value,
                "annotation_revision": revisions[label.clip_id],
            }
            for label in labels
            if label.clip_id is not None
        ],
    }
    db.add(SystemSetting(key=f"{BENCHMARK_PREFIX}{benchmark_id}", value=value, updated_by=actor.id))
    await db.flush()
    return value


async def validate_benchmark_current(db: AsyncSession, benchmark: SystemSetting) -> None:
    samples = benchmark.value["samples"]
    clip_ids = {uuid.UUID(sample["clip_id"]) for sample in samples}
    clips = (await db.execute(select(Clip).where(Clip.id.in_(clip_ids)))).scalars().all()
    by_id = {str(clip.id): clip for clip in clips}
    for sample in samples:
        clip = by_id.get(sample["clip_id"])
        if (
            clip is None
            or not clip.is_reviewed
            or annotation_revision(clip) != sample.get("annotation_revision", 0)
        ):
            raise HTTPException(
                422, detail="Benchmark truth changed; build a reviewed benchmark version"
            )


async def evaluate_model(
    db: AsyncSession,
    *,
    model_id: uuid.UUID,
    benchmark_id: uuid.UUID,
    prediction_label_ids: list[uuid.UUID],
    actor: User,
) -> dict[str, Any]:
    model = (
        await db.execute(select(ModelVersion).where(ModelVersion.id == model_id).with_for_update())
    ).scalar_one_or_none()
    benchmark = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.key == f"{BENCHMARK_PREFIX}{benchmark_id}")
        )
    ).scalar_one_or_none()
    if model is None or benchmark is None:
        raise HTTPException(404, detail="Model or benchmark not found")
    await validate_benchmark_current(db, benchmark)
    samples = benchmark.value["samples"]
    holdout_clips = {uuid.UUID(sample["clip_id"]) for sample in samples}
    artifact_sha256, fitted_clips = await to_thread.run_sync(_artifact_info, model.artifact_uri)
    if fitted_clips is not None:
        if holdout_clips.intersection(fitted_clips):
            raise HTTPException(
                422, detail="Benchmark overlaps model training or calibration clips"
            )
    elif model.training_dataset_id:
        dataset = await db.get(TrainingDataset, model.training_dataset_id)
        source_ids = [uuid.UUID(raw) for raw in dataset.source_label_ids] if dataset else []
        training_clips = (
            (await db.execute(select(Label.clip_id).where(Label.id.in_(source_ids))))
            .scalars()
            .all()
        )
        if holdout_clips.intersection(training_clips):
            raise HTTPException(422, detail="Benchmark overlaps this model's training data")
    ids = list(dict.fromkeys(prediction_label_ids))
    predictions = list((await db.execute(select(Label).where(Label.id.in_(ids)))).scalars().all())
    if len(predictions) != len(ids):
        raise HTTPException(422, detail="Prediction labels must exist")
    by_target: dict[tuple[str, str | None, str], Label] = {}
    for label in predictions:
        value = label.label_value or {}
        provenance = value.get("provenance", {})
        source = (
            provenance.get("source", value.get("source")) if isinstance(provenance, dict) else None
        )
        if label.source != "model" or label.model_version_id != model.id or source != "real":
            raise HTTPException(
                422, detail="Evaluation requires persisted real predictions from this model"
            )
        target = (
            str(label.clip_id),
            str(label.tracklet_id) if label.tracklet_id else None,
            canonical_label_type(label.label_type),
        )
        if target in by_target:
            raise HTTPException(422, detail="Duplicate prediction target")
        by_target[target] = label
    truth_classes: list[str] = []
    predicted_classes: list[str] = []
    used_ids: list[str] = []
    for sample in samples:
        target = (
            sample["clip_id"],
            sample["tracklet_id"],
            canonical_label_type(sample["label_type"]),
        )
        prediction = by_target.get(target)
        truth_classes.append(normalized_label(sample["truth"], sample["label_type"]))
        predicted_classes.append(
            normalized_label(prediction.label_value, prediction.label_type)
            if prediction
            else "__abstained__"
        )
        if prediction:
            used_ids.append(str(prediction.id))
    total = len(samples)
    correct = sum(t == p for t, p in zip(truth_classes, predicted_classes, strict=True))
    counts = Counter(truth_classes)
    scores = []
    for category, support in counts.items():
        true_positive = sum(
            t == p == category for t, p in zip(truth_classes, predicted_classes, strict=True)
        )
        predicted = predicted_classes.count(category)
        scores.append(2 * true_positive / (support + predicted))
    accuracy = correct / total
    macro_f1 = sum(scores) / len(scores)
    baseline_accuracy = None
    training_snapshot_sha256 = None
    if model.training_dataset_id:
        training_dataset = await db.get(TrainingDataset, model.training_dataset_id)
        if training_dataset is None:
            raise HTTPException(422, detail="This candidate's training snapshot is unavailable")
        frozen_classes, training_snapshot_sha256 = await to_thread.run_sync(
            _training_truths, training_dataset
        )
        train_classes = Counter(frozen_classes)
        if train_classes:
            majority_class = train_classes.most_common(1)[0][0]
            baseline_accuracy = truth_classes.count(majority_class) / total
    baseline_gate = baseline_accuracy is None or accuracy > baseline_accuracy
    evaluation_id = uuid.uuid4()
    evaluation = {
        "id": str(evaluation_id),
        "model_id": str(model.id),
        "benchmark_id": str(benchmark_id),
        "sample_count": total,
        "prediction_count": len(used_ids),
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "baseline_accuracy": baseline_accuracy,
        "baseline_gate": baseline_gate,
        "training_snapshot_sha256": training_snapshot_sha256,
        "source": "measured_human_holdout",
        "artifact_sha256": artifact_sha256,
        "prediction_label_ids": used_ids,
        "passed": total >= MIN_SAMPLES
        and accuracy >= MIN_ACCURACY
        and macro_f1 >= MIN_MACRO_F1
        and baseline_gate,
        "thresholds": {
            "sample_count": MIN_SAMPLES,
            "accuracy": MIN_ACCURACY,
            "macro_f1": MIN_MACRO_F1,
        },
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": str(actor.id),
    }
    db.add(
        SystemSetting(
            key=f"{EVALUATION_PREFIX}{evaluation_id}", value=evaluation, updated_by=actor.id
        )
    )
    model.metrics = {
        **(model.metrics or {}),
        "evaluation_id": str(evaluation_id),
        "artifact_sha256": artifact_sha256,
        "benchmark": evaluation,
    }
    await db.flush()
    return evaluation


async def promote_model(
    db: AsyncSession, *, model: ModelVersion, stage: ModelStage, actor: User
) -> ModelVersion:
    if stage in (ModelStage.staging, ModelStage.production):
        evaluation_id = (model.metrics or {}).get("evaluation_id")
        evaluation = (
            (
                await db.execute(
                    select(SystemSetting).where(
                        SystemSetting.key == f"{EVALUATION_PREFIX}{evaluation_id}"
                    )
                )
            ).scalar_one_or_none()
            if evaluation_id
            else None
        )
        if (
            evaluation is None
            or not evaluation.value.get("passed")
            or evaluation.value.get("model_id") != str(model.id)
        ):
            raise HTTPException(
                422, detail="Promotion requires a passing measured human-holdout evaluation"
            )
        if not model.artifact_uri:
            raise HTTPException(422, detail="Promotion requires a registered model artifact")
        benchmark = await db.get(
            SystemSetting, f"{BENCHMARK_PREFIX}{evaluation.value['benchmark_id']}"
        )
        if benchmark is None:
            raise HTTPException(422, detail="Release benchmark is unavailable")
        await validate_benchmark_current(db, benchmark)
        actual_sha256, _fitted_clips = await to_thread.run_sync(_artifact_info, model.artifact_uri)
        if actual_sha256 != evaluation.value.get("artifact_sha256"):
            raise HTTPException(
                422, detail="Model artifact changed after evaluation; evaluate it again"
            )
        expected_training_sha = evaluation.value.get("training_snapshot_sha256")
        if expected_training_sha:
            training_dataset = await db.get(TrainingDataset, model.training_dataset_id)
            if training_dataset is None:
                raise HTTPException(422, detail="Release training snapshot is unavailable")
            _classes, actual_training_sha = await to_thread.run_sync(
                _training_truths, training_dataset
            )
            if actual_training_sha != expected_training_sha:
                raise HTTPException(422, detail="Training snapshot changed after evaluation")
    previous = model.promoted_stage
    if stage == ModelStage.production:
        others = (
            (
                await db.execute(
                    select(ModelVersion)
                    .where(
                        ModelVersion.model_name == model.model_name,
                        ModelVersion.promoted_stage == ModelStage.production,
                        ModelVersion.id != model.id,
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for other in others:
            other.promoted_stage = ModelStage.retired
    model.promoted_stage = stage
    action_id = uuid.uuid4()
    db.add(
        SystemSetting(
            key=f"learning:promotion:{action_id}",
            value={
                "model_id": str(model.id),
                "from": previous.value,
                "to": stage.value,
                "actor_id": str(actor.id),
                "at": datetime.now(UTC).isoformat(),
            },
            updated_by=actor.id,
        )
    )
    await db.flush()
    return model
