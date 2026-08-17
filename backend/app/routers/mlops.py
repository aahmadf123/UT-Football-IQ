"""MLOps router — model registry, active learning queue, and nightly loop."""

import json
import uuid
from collections import Counter, defaultdict
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.active_learning import (
    annotation_priority,
    basis_for,
    parse_uncertainty,
    signal_from_label_value,
)
from app.config import get_settings
from app.database import get_db
from app.deps import require_admin, require_analyst_or_above
from app.models import (
    ActiveLearningQueueItem,
    ActiveLearningReason,
    ActiveLearningStatus,
    Clip,
    CoachCorrection,
    JobStatus,
    JobType,
    Label,
    ModelStage,
    ModelVersion,
    ProcessingJob,
    TrainingDataset,
    User,
)
from app.utils import make_dataset_artifact_uri

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/mlops", tags=["mlops"])


def _confidence(value: dict[str, Any] | None) -> float | None:
    if value is None:
        return None
    raw = value.get("confidence")
    if isinstance(raw, int | float):
        return float(raw)
    return None


def _normalized_value(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ModelVersionCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: str = Field(min_length=1, max_length=255)
    version: str = Field(min_length=1, max_length=100)
    model_type: str = Field(min_length=1, max_length=100)
    artifact_uri: str | None = None
    training_dataset_id: uuid.UUID | None = None
    metrics: dict[str, Any] | None = None


class ModelVersionResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    model_name: str
    version: str
    model_type: str
    artifact_uri: str | None
    training_dataset_id: uuid.UUID | None
    metrics: dict[str, Any] | None
    promoted_stage: ModelStage


class PromoteModelRequest(BaseModel):
    stage: ModelStage


class NightlyRunResponse(BaseModel):
    exported_corrections: int
    queued_low_confidence: int
    queued_uncertainty: int
    queued_regressions: int
    queued_hard_negatives: int
    training_dataset_id: uuid.UUID | None
    ingestion_job_id: uuid.UUID
    evaluation_job_id: uuid.UUID


class ActiveLearningQueueItemResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    clip_id: uuid.UUID | None
    label_type: str
    queue_reason: ActiveLearningReason
    priority_score: float
    # ``model_confidence`` is calibration-honest (#146): populated ONLY when the
    # stored uncertainty signal is calibrated. An uncalibrated raw score is never
    # surfaced here as a confidence — use ``calibrated`` / ``basis`` instead.
    model_confidence: float | None
    calibrated: bool
    entropy: float | None
    basis: str
    uncertainty_method: str | None
    source_model_version_id: uuid.UUID | None
    status: str
    created_at: str

    @classmethod
    def from_orm_item(cls, item: ActiveLearningQueueItem) -> "ActiveLearningQueueItemResponse":
        meta = item.metadata_ if isinstance(item.metadata_, dict) else None
        signal = parse_uncertainty(meta.get("uncertainty") if meta is not None else None)
        return cls(
            id=item.id,
            clip_id=item.clip_id,
            label_type=item.label_type,
            queue_reason=item.queue_reason,
            priority_score=item.priority_score,
            model_confidence=(
                signal.confidence if signal is not None and signal.calibrated else None
            ),
            calibrated=signal.calibrated if signal is not None else False,
            entropy=signal.entropy if signal is not None else None,
            basis=basis_for(signal),
            uncertainty_method=signal.method if signal is not None else None,
            source_model_version_id=item.source_model_version_id,
            status=item.status.value,
            created_at=item.created_at.isoformat(),
        )


@router.get("/models", response_model=list[ModelVersionResponse])
async def list_model_versions(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
    model_name: str | None = Query(default=None),
    model_type: str | None = Query(default=None),
    stage: ModelStage | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ModelVersionResponse]:
    """List registered model versions, optionally filtered by name/type/stage.

    ``model_type`` + ``stage=production`` is the GPU worker's serving query:
    it resolves the artifact to load for a pipeline stage (see
    gpu-worker/pipeline/model_registry_client.py).
    """
    q = select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(limit)
    if model_name is not None:
        q = q.where(ModelVersion.model_name == model_name)
    if model_type is not None:
        q = q.where(ModelVersion.model_type == model_type)
    if stage is not None:
        q = q.where(ModelVersion.promoted_stage == stage)
    result = await db.execute(q)
    models = result.scalars().all()
    return [ModelVersionResponse.model_validate(m, from_attributes=True) for m in models]


@router.get("/models/{model_version_id}", response_model=ModelVersionResponse)
async def get_model_version(
    model_version_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
) -> ModelVersionResponse:
    """Get a single model version by ID."""
    result = await db.execute(select(ModelVersion).where(ModelVersion.id == model_version_id))
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model version not found")
    return ModelVersionResponse.model_validate(model, from_attributes=True)


@router.post("/models", response_model=ModelVersionResponse, status_code=status.HTTP_201_CREATED)
async def create_model_version(
    body: ModelVersionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
) -> ModelVersionResponse:
    if body.training_dataset_id is not None:
        dataset_result = await db.execute(
            select(TrainingDataset).where(TrainingDataset.id == body.training_dataset_id)
        )
        if dataset_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Training dataset not found"
            )

    model = ModelVersion(
        id=uuid.uuid4(),
        model_name=body.model_name,
        version=body.version,
        model_type=body.model_type,
        artifact_uri=body.artifact_uri,
        training_dataset_id=body.training_dataset_id,
        metrics=body.metrics,
    )
    db.add(model)
    await db.flush()
    return ModelVersionResponse.model_validate(model, from_attributes=True)


@router.post("/models/{model_version_id}/promote", response_model=ModelVersionResponse)
async def promote_model_version(
    model_version_id: uuid.UUID,
    body: PromoteModelRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_admin)],
) -> ModelVersionResponse:
    result = await db.execute(select(ModelVersion).where(ModelVersion.id == model_version_id))
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model version not found")

    if body.stage in {ModelStage.staging, ModelStage.production} and not model.metrics:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Validation metrics are required before promoting to staging or production",
        )
    model.promoted_stage = body.stage
    await db.flush()
    return ModelVersionResponse.model_validate(model, from_attributes=True)


@router.get("/datasets", response_model=list[dict[str, Any]])
async def list_training_datasets(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
    model_scope: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    q = select(TrainingDataset).order_by(TrainingDataset.snapshot_date.desc()).limit(50)
    if model_scope is not None:
        q = q.where(TrainingDataset.model_scope == model_scope)
    result = await db.execute(q)
    datasets = result.scalars().all()
    return [
        {
            "id": d.id,
            "snapshot_date": d.snapshot_date.isoformat(),
            "model_scope": d.model_scope,
            "row_count": d.row_count,
            "artifact_uri": d.artifact_uri,
            "source_label_ids": d.source_label_ids,
            "source_correction_ids": d.source_correction_ids,
            "changelog": d.changelog,
        }
        for d in datasets
    ]


@router.get("/active-learning/queue", response_model=list[ActiveLearningQueueItemResponse])
async def list_active_learning_queue(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
    reason: ActiveLearningReason | None = Query(default=None),
    status_filter: str = Query(default="queued"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ActiveLearningQueueItemResponse]:
    q = (
        select(ActiveLearningQueueItem)
        .where(ActiveLearningQueueItem.status == status_filter)
        .order_by(
            ActiveLearningQueueItem.priority_score.desc(), ActiveLearningQueueItem.created_at.desc()
        )
        .limit(limit)
    )
    if reason is not None:
        q = q.where(ActiveLearningQueueItem.queue_reason == reason)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [ActiveLearningQueueItemResponse.from_orm_item(r) for r in rows]


class ActiveLearningStatusUpdate(BaseModel):
    """Body for the queue-item status transition endpoint."""

    status: ActiveLearningStatus


@router.patch(
    "/active-learning/queue/{item_id}",
    response_model=ActiveLearningQueueItemResponse,
)
async def update_active_learning_item_status(
    item_id: uuid.UUID,
    body: ActiveLearningStatusUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
) -> ActiveLearningQueueItemResponse:
    """Advance a queue item's status (idempotent).

    The active-learning exporter marks items ``in_review`` after their frames
    upload to the labeling workspace; without this transition, every export
    run would re-fetch the same top-priority items forever and starve the
    rest of the queue.
    """
    result = await db.execute(
        select(ActiveLearningQueueItem).where(ActiveLearningQueueItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Queue item not found")
    if item.status != body.status:
        item.status = body.status
        await db.flush()
    return ActiveLearningQueueItemResponse.from_orm_item(item)


@router.post("/active-learning/nightly", response_model=NightlyRunResponse)
async def run_nightly_active_learning(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_analyst_or_above)],
    high_confidence_threshold: float = Query(default=0.9, ge=0.0, le=1.0),
    low_confidence_threshold: float = Query(default=0.6, ge=0.0, le=1.0),
    uncertainty_per_label_type: int = Query(default=3, ge=1, le=20),
    model_scope: str = Query(default="general"),
) -> NightlyRunResponse:
    ingestion_job = ProcessingJob(
        id=uuid.uuid4(),
        job_type=JobType.labels,
        status=JobStatus.running,
        output_artifacts={"step": "nightly_correction_ingestion"},
    )
    db.add(ingestion_job)
    await db.flush()

    corrections_result = await db.execute(
        select(CoachCorrection).where(
            CoachCorrection.training_eligible.is_(True),
            CoachCorrection.exported_as_label.is_(False),
        )
    )
    corrections = corrections_result.scalars().all()

    exported_labels: list[Label] = []
    exported_correction_ids: list[uuid.UUID] = []
    for correction in corrections:
        confidence = _confidence(correction.original_value)
        if confidence is not None and confidence < high_confidence_threshold:
            continue

        clip_result = await db.execute(select(Clip).where(Clip.id == correction.clip_id))
        clip = clip_result.scalar_one_or_none()
        label = Label(
            id=uuid.uuid4(),
            clip_id=correction.clip_id,
            label_type=correction.correction_type.value,
            label_value=correction.corrected_value,
            annotated_by=correction.corrected_by,
            source="human",
            model_version_id=clip.model_version_id if clip is not None else None,
        )
        db.add(label)
        correction.exported_as_label = True
        exported_labels.append(label)
        exported_correction_ids.append(correction.id)

    training_dataset_id: uuid.UUID | None = None
    if exported_labels:
        previous_result = await db.execute(
            select(TrainingDataset)
            .where(TrainingDataset.model_scope == model_scope)
            .order_by(TrainingDataset.snapshot_date.desc())
            .limit(1)
        )
        previous = previous_result.scalar_one_or_none()
        previous_corrections = (
            {uuid.UUID(c) for c in previous.source_correction_ids}
            if previous is not None
            else set()
        )
        added_corrections = [
            str(c_id) for c_id in exported_correction_ids if c_id not in previous_corrections
        ]

        training_dataset = TrainingDataset(
            id=uuid.uuid4(),
            model_scope=model_scope,
            source_label_ids=[str(label.id) for label in exported_labels],
            source_correction_ids=[str(c_id) for c_id in exported_correction_ids],
            row_count=len(exported_labels),
            artifact_uri=make_dataset_artifact_uri(model_scope),
            changelog={"added_correction_ids": added_corrections},
            created_by=current_user.id,
        )
        db.add(training_dataset)
        await db.flush()
        training_dataset_id = training_dataset.id
        for label in exported_labels:
            label.training_dataset_id = training_dataset_id
            label.dataset_version = str(training_dataset_id)

    ingestion_job.status = JobStatus.succeeded
    ingestion_job.training_dataset_id = training_dataset_id
    ingestion_job.output_artifacts = {
        "step": "nightly_correction_ingestion",
        "training_dataset_id": str(training_dataset_id) if training_dataset_id else None,
        "exported_count": len(exported_labels),
    }

    model_labels_result = await db.execute(select(Label).where(Label.source == "model"))
    model_labels = model_labels_result.scalars().all()

    queue_rows: list[ActiveLearningQueueItem] = []
    low_confidence_count = 0
    uncertainty_count = 0
    regression_count = 0
    hard_negative_count = 0

    al_settings = get_settings()
    for label in model_labels:
        confidence = _confidence(label.label_value)
        if confidence is not None and confidence < low_confidence_threshold:
            # Route the priority through the calibrated-uncertainty policy (#146):
            # a calibrated ``label_value["uncertainty"]`` drives a calibrated
            # priority; a bare confidence is demoted to a ranking-only raw score
            # and is never re-presented as a calibrated confidence.
            signal = signal_from_label_value(label.label_value)
            decision = annotation_priority(
                signal,
                entropy_weight=al_settings.active_learning_entropy_weight,
                uncalibrated_priority=al_settings.active_learning_uncalibrated_priority,
                missing_priority=al_settings.active_learning_missing_priority,
            )
            queue_rows.append(
                ActiveLearningQueueItem(
                    id=uuid.uuid4(),
                    clip_id=label.clip_id,
                    label_type=label.label_type,
                    queue_reason=decision.reason,
                    priority_score=decision.priority,
                    # Raw score kept in the column for audit; the API only
                    # surfaces it when calibrated.
                    model_confidence=confidence,
                    source_model_version_id=label.model_version_id,
                    metadata_=(
                        {"uncertainty": signal.as_payload()} if signal is not None else None
                    ),
                )
            )
            low_confidence_count += 1

    label_buckets: dict[str, list[Label]] = defaultdict(list)
    for label in model_labels:
        label_buckets[label.label_type].append(label)
    repeated_label_types = {
        label_type
        for label_type, count in Counter(label.label_type for label in model_labels).items()
        if count > 1
    }
    for label_type in repeated_label_types:
        ranked = sorted(
            label_buckets[label_type],
            key=lambda label: _confidence(label.label_value) or 1.0,
        )[:uncertainty_per_label_type]
        for label in ranked:
            confidence = _confidence(label.label_value)
            queue_rows.append(
                ActiveLearningQueueItem(
                    id=uuid.uuid4(),
                    clip_id=label.clip_id,
                    label_type=label.label_type,
                    queue_reason=ActiveLearningReason.uncertainty_sampling,
                    priority_score=1 - confidence if confidence is not None else 1.0,
                    model_confidence=confidence,
                    source_model_version_id=label.model_version_id,
                )
            )
            uncertainty_count += 1

    corrected_labels_result = await db.execute(select(Label).where(Label.source == "human"))
    corrected_labels = corrected_labels_result.scalars().all()
    corrected_map = {
        (label.clip_id, label.label_type): _normalized_value(label.label_value)
        for label in corrected_labels
    }
    for label in model_labels:
        key = (label.clip_id, label.label_type)
        corrected_value = corrected_map.get(key)
        if corrected_value is None:
            continue
        predicted_value = _normalized_value(label.label_value)
        if predicted_value == corrected_value:
            continue

        confidence = _confidence(label.label_value)
        queue_rows.append(
            ActiveLearningQueueItem(
                id=uuid.uuid4(),
                clip_id=label.clip_id,
                label_type=label.label_type,
                queue_reason=ActiveLearningReason.regression,
                priority_score=1.0 if confidence is None else min(1.0, 1 - confidence + 0.2),
                model_confidence=confidence,
                source_model_version_id=label.model_version_id,
                metadata_={"corrected_value": corrected_value},
            )
        )
        regression_count += 1
        if confidence is not None and confidence >= high_confidence_threshold:
            queue_rows.append(
                ActiveLearningQueueItem(
                    id=uuid.uuid4(),
                    clip_id=label.clip_id,
                    label_type=label.label_type,
                    queue_reason=ActiveLearningReason.hard_negative,
                    priority_score=min(1.0, confidence),
                    model_confidence=confidence,
                    source_model_version_id=label.model_version_id,
                    metadata_={"corrected_value": corrected_value},
                )
            )
            hard_negative_count += 1

    for row in queue_rows:
        db.add(row)

    evaluation_job = ProcessingJob(
        id=uuid.uuid4(),
        job_type=JobType.metrics,
        status=JobStatus.succeeded,
        training_dataset_id=training_dataset_id,
        output_artifacts={
            "step": "nightly_evaluation_and_regression",
            "queued_low_confidence": low_confidence_count,
            "queued_uncertainty": uncertainty_count,
            "queued_regressions": regression_count,
            "queued_hard_negatives": hard_negative_count,
            "regression_alert": regression_count > 0,
        },
    )
    db.add(evaluation_job)
    await db.flush()

    log.info(
        "nightly_active_learning_completed",
        exported_corrections=len(exported_labels),
        queued_low_confidence=low_confidence_count,
        queued_uncertainty=uncertainty_count,
        queued_regressions=regression_count,
        queued_hard_negatives=hard_negative_count,
        ingestion_job_id=str(ingestion_job.id),
        evaluation_job_id=str(evaluation_job.id),
    )
    return NightlyRunResponse(
        exported_corrections=len(exported_labels),
        queued_low_confidence=low_confidence_count,
        queued_uncertainty=uncertainty_count,
        queued_regressions=regression_count,
        queued_hard_negatives=hard_negative_count,
        training_dataset_id=training_dataset_id,
        ingestion_job_id=ingestion_job.id,
        evaluation_job_id=evaluation_job.id,
    )
