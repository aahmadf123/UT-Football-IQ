"""Local human-label snapshots and measured model evaluation; no hosted training."""

import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import require_admin, require_analyst_or_above
from app.models import (
    JobStatus,
    JobType,
    Label,
    ModelStage,
    ModelVersion,
    PipelineMode,
    ProcessingJob,
    SystemSetting,
    TrainingDataset,
    User,
)
from app.services.learning import create_benchmark, evaluate_model, promote_model
from app.storage import (
    ARTIFACTS,
    local_object_path,
    parse_storage_uri,
    sign_stream_path,
    verify_stream_signature,
)
from app.workload import require_workload_capacity

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


class BenchmarkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    truth_label_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)


class EvaluationCreate(BaseModel):
    model_id: uuid.UUID
    benchmark_id: uuid.UUID
    prediction_label_ids: list[uuid.UUID] = Field(min_length=1, max_length=2000)


class TrainingCreate(BaseModel):
    training_variant: Literal["local_coverage"] = "local_coverage"
    training_dataset_id: uuid.UUID
    features_uri: str = Field(min_length=1, max_length=2048)


class SessionSplit(BaseModel):
    session_id: uuid.UUID
    split: Literal["train", "calibration", "holdout"]


class ManifestCreate(BaseModel):
    name: str = Field(default="Coverage learning cohort", min_length=1, max_length=120)
    sessions: list[SessionSplit] = Field(min_length=3, max_length=100)


class CandidateEvaluationCreate(BaseModel):
    benchmark_id: uuid.UUID


@router.post("/feature-manifests/preview")
async def manifest_preview(
    body: ManifestCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    from app.services.learning_manifests import preview_manifest

    return await preview_manifest(
        db, sessions=[item.model_dump() for item in body.sessions], name=body.name
    )


@router.post("/feature-manifests", status_code=201)
async def build_manifest(
    body: ManifestCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    from app.services.learning_manifests import create_manifest

    return await create_manifest(
        db, sessions=[item.model_dump() for item in body.sessions], name=body.name, actor=user
    )


@router.get("/feature-manifests")
async def manifests(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(SystemSetting)
                .where(SystemSetting.key.startswith("learning:manifest:"))
                .order_by(SystemSetting.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [row.value for row in rows]


@router.get("/candidates")
async def candidates(
    manifest_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> list[dict[str, Any]]:
    manifest = await db.get(SystemSetting, f"learning:manifest:{manifest_id}")
    if manifest is None:
        raise HTTPException(404, detail="Learning manifest not found")
    dataset_id = uuid.UUID(manifest.value["training_dataset_id"])
    benchmark = await db.get(SystemSetting, f"learning:benchmark:{manifest.value['benchmark_id']}")
    if benchmark is None:
        raise HTTPException(422, detail="Manifest benchmark is unavailable")
    clip_ids = [uuid.UUID(sample["clip_id"]) for sample in benchmark.value["samples"]]
    models = (
        (
            await db.execute(
                select(ModelVersion)
                .where(ModelVersion.training_dataset_id == dataset_id)
                .order_by(ModelVersion.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    output = []
    for model in models:
        count = (
            await db.execute(
                select(func.count(func.distinct(Label.clip_id))).where(
                    Label.model_version_id == model.id,
                    Label.clip_id.in_(clip_ids),
                    Label.source == "model",
                    Label.label_value["provenance"]["source"].astext == "real",
                )
            )
        ).scalar_one()
        output.append(
            {
                "id": str(model.id),
                "model_name": model.model_name,
                "version": model.version,
                "model_type": model.model_type,
                "artifact_uri": model.artifact_uri,
                "metrics": model.metrics,
                "promoted_stage": model.promoted_stage.value,
                "prediction_count": count,
                "required_predictions": len(clip_ids),
                "ready_for_evaluation": count >= len(clip_ids) and bool(model.artifact_uri),
            }
        )
    return output


@router.post("/candidates/{model_id}/evaluate", status_code=201)
async def evaluate_candidate(
    model_id: uuid.UUID,
    body: CandidateEvaluationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    benchmark = await db.get(SystemSetting, f"learning:benchmark:{body.benchmark_id}")
    if benchmark is None:
        raise HTTPException(404, detail="Benchmark not found")
    clip_ids = [uuid.UUID(sample["clip_id"]) for sample in benchmark.value["samples"]]
    ids = (
        (
            await db.execute(
                select(Label.id).where(
                    Label.model_version_id == model_id,
                    Label.clip_id.in_(clip_ids),
                    Label.source == "model",
                    Label.label_value["provenance"]["source"].astext == "real",
                )
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        raise HTTPException(422, detail="This candidate has no real holdout predictions yet")
    return await evaluate_model(
        db,
        model_id=model_id,
        benchmark_id=body.benchmark_id,
        prediction_label_ids=list(ids),
        actor=user,
    )


@router.post("/train", status_code=201)
async def train(
    body: TrainingCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, str]:
    dataset = (
        await db.execute(
            select(TrainingDataset)
            .where(TrainingDataset.id == body.training_dataset_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if dataset is None or not dataset.artifact_uri:
        raise HTTPException(404, detail="Materialized training dataset not found")
    combined_uri = (dataset.changelog or {}).get("combined_labels_uri", dataset.artifact_uri)
    for uri in (dataset.artifact_uri, combined_uri, body.features_uri):
        try:
            scheme, bucket, key = parse_storage_uri(uri)
            if (
                scheme != "local"
                or bucket != ARTIFACTS
                or not local_object_path(bucket, key).is_file()
            ):
                raise ValueError("Missing local artifact")
        except ValueError as exc:
            raise HTTPException(
                422, detail="Training requires existing local dataset and feature artifacts"
            ) from exc
    active = (
        (
            await db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.job_type == JobType.train,
                    ProcessingJob.training_dataset_id == dataset.id,
                    ProcessingJob.status.in_(
                        (JobStatus.queued, JobStatus.running, JobStatus.paused)
                    ),
                    ProcessingJob.input_artifacts["training_variant"].astext
                    == body.training_variant,
                    ProcessingJob.input_artifacts["features_uri"].astext == body.features_uri,
                )
            )
        )
        .scalars()
        .first()
    )
    if active is not None:
        return {
            "job_id": str(active.id),
            "status": active.status.value,
            "training_variant": body.training_variant,
        }
    await require_workload_capacity("learning.train")(user=_user, db=db)
    job = ProcessingJob(
        id=uuid.uuid4(),
        job_type=JobType.train,
        status=JobStatus.queued,
        priority=0,
        pipeline_mode=PipelineMode.nightly,
        training_dataset_id=dataset.id,
        input_artifacts={
            "training_variant": body.training_variant,
            "training_dataset_id": str(dataset.id),
            "labels_uri": combined_uri,
            "features_uri": body.features_uri,
        },
    )
    db.add(job)
    await db.flush()
    return {"job_id": str(job.id), "status": "queued", "training_variant": body.training_variant}


@router.post("/benchmarks", status_code=201)
async def benchmark(
    body: BenchmarkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    return await create_benchmark(db, label_ids=body.truth_label_ids, name=body.name, actor=user)


@router.get("/benchmarks")
async def benchmarks(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(SystemSetting)
                .where(SystemSetting.key.startswith("learning:benchmark:"))
                .order_by(SystemSetting.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [row.value for row in rows]


@router.post("/evaluations", status_code=201)
async def evaluation(
    body: EvaluationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    return await evaluate_model(
        db,
        model_id=body.model_id,
        benchmark_id=body.benchmark_id,
        prediction_label_ids=body.prediction_label_ids,
        actor=user,
    )


@router.get("/evaluations")
async def evaluations(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(SystemSetting)
                .where(SystemSetting.key.startswith("learning:evaluation:"))
                .order_by(SystemSetting.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [row.value for row in rows]


@router.post("/models/{model_id}/rollback")
async def rollback(
    model_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_admin)],
) -> dict[str, str]:
    model = (
        await db.execute(select(ModelVersion).where(ModelVersion.id == model_id).with_for_update())
    ).scalar_one_or_none()
    if model is None:
        raise HTTPException(404, detail="Model not found")
    await promote_model(db, model=model, stage=ModelStage.production, actor=user)
    return {"model_id": str(model.id), "stage": model.promoted_stage.value}


@router.get("/datasets/{dataset_id}/download")
async def dataset_download(
    dataset_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, str]:
    dataset = await db.get(TrainingDataset, dataset_id)
    if dataset is None or not dataset.artifact_uri:
        raise HTTPException(404, detail="Dataset not found")
    scheme, bucket, key = parse_storage_uri(dataset.artifact_uri)
    if scheme != "local" or bucket != ARTIFACTS:
        raise HTTPException(
            422, detail="Import this snapshot into the local library before downloading"
        )
    exp = int(time.time()) + get_settings().s3_presign_ttl
    sig = sign_stream_path(bucket, key, exp)
    base = str(request.base_url).rstrip("/")
    url = f"{base}/api/v1/learning/datasets/{dataset_id}/file?exp={exp}&sig={sig}"
    return {"download_url": url}


@router.get("/datasets/{dataset_id}/file", include_in_schema=False)
async def dataset_file(
    dataset_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    exp: str = Query(...),
    sig: str = Query(...),
) -> FileResponse:
    dataset = await db.get(TrainingDataset, dataset_id)
    if dataset is None or not dataset.artifact_uri:
        raise HTTPException(404, detail="Dataset not found")
    scheme, bucket, key = parse_storage_uri(dataset.artifact_uri)
    if (
        scheme != "local"
        or bucket != ARTIFACTS
        or not verify_stream_signature(bucket, key, exp, sig)
    ):
        raise HTTPException(403, detail="Invalid or expired dataset link")
    path = local_object_path(bucket, key)
    if not path.is_file():
        raise HTTPException(404, detail="Snapshot artifact not found")
    return FileResponse(path, media_type="application/x-ndjson", filename=f"{dataset_id}.jsonl")
