"""Real PostgreSQL acceptance for corrections, replay, snapshots and release gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.config import get_settings
from app.models import (
    Clip,
    CoachCorrection,
    CorrectionType,
    FieldCalibration,
    Label,
    Metric,
    ModelStage,
    ModelVersion,
    ProcessingJob,
    SystemSetting,
    Tracklet,
    TrainingDataset,
    User,
    UserRole,
    Video,
)
from app.services.annotations import annotation_revision, submit_correction
from app.services.corrections_export import export_corrections
from app.services.learning import create_benchmark, evaluate_model, promote_model
from app.services.metric_policy import metric_visibility
from app.storage import local_object_path, parse_storage_uri
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def local_library() -> AsyncGenerator[
    tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID], None
]:
    engine = create_async_engine(get_settings().database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    actor = User(
        id=uuid.uuid4(),
        email=f"annotations-{uuid.uuid4()}@test.local",
        hashed_password="x",
        full_name="Annotation coach",
        role=UserRole.admin,
    )
    video = Video(
        id=uuid.uuid4(),
        filename="real-film.mp4",
        storage_uri="local://raw-video/real-film.mp4",
        uploaded_by=actor.id,
        duration_seconds=20,
    )
    async with maker() as db:
        db.add_all([actor, video])
        await db.commit()
    try:
        yield maker, actor.id, video.id
    finally:
        async with maker() as db:
            clip_ids = select(Clip.id).where(Clip.video_id == video.id)
            model_ids = list(
                (
                    await db.execute(
                        select(Label.model_version_id).where(
                            Label.clip_id.in_(clip_ids), Label.model_version_id.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            await db.execute(delete(Label).where(Label.clip_id.in_(clip_ids)))
            await db.execute(delete(Video).where(Video.id == video.id))
            if model_ids:
                await db.execute(delete(ModelVersion).where(ModelVersion.id.in_(model_ids)))
            dataset_ids = select(TrainingDataset.id).where(TrainingDataset.created_by == actor.id)
            await db.execute(
                delete(ProcessingJob).where(ProcessingJob.training_dataset_id.in_(dataset_ids))
            )
            await db.execute(delete(TrainingDataset).where(TrainingDataset.created_by == actor.id))
            await db.execute(delete(SystemSetting).where(SystemSetting.updated_by == actor.id))
            await db.execute(delete(User).where(User.id == actor.id))
            await db.commit()
        await engine.dispose()


async def seed_clip(maker: async_sessionmaker[AsyncSession], video_id: uuid.UUID) -> uuid.UUID:
    async with maker() as db:
        clip = Clip(
            id=uuid.uuid4(),
            video_id=video_id,
            start_time=2,
            end_time=8,
            storage_uri="local://clips/old.mp4",
            label_data={},
            provenance={"source": "real"},
        )
        db.add(clip)
        db.add(
            Metric(
                id=uuid.uuid4(),
                clip_id=clip.id,
                metric_name="distance_yards",
                metric_value={"value": 10},
                analytics_safe=True,
            )
        )
        await db.commit()
        return clip.id


@pytest.mark.asyncio
async def test_correction_applies_now_and_persists_after_new_session(local_library) -> None:
    maker, actor_id, video_id = local_library
    clip_id = await seed_clip(maker, video_id)
    async with maker() as db:
        actor = await db.get(User, actor_id)
        await submit_correction(
            db,
            actor=actor,
            clip_id=clip_id,
            correction_type=CorrectionType.formation_tag,
            corrected_value={"formation": "trips"},
            expected_revision=0,
        )
        await submit_correction(
            db,
            actor=actor,
            clip_id=clip_id,
            correction_type=CorrectionType.clip_boundary,
            corrected_value={"start_seconds": 3, "end_seconds": 9},
            expected_revision=1,
        )
        await db.commit()
    async with maker() as db:
        clip = await db.get(Clip, clip_id)
        assert (clip.start_time, clip.end_time) == (3, 9)
        assert clip.storage_uri is None
        assert annotation_revision(clip) == 2
        assert clip.label_data["_observation_revision"] == 1
        assert clip.is_reviewed is False
        label = (await db.execute(select(Label).where(Label.clip_id == clip_id))).scalar_one()
        assert label.label_value == {"formation": "trips"}
        assert label.source == "human"
        metric = (await db.execute(select(Metric).where(Metric.clip_id == clip_id))).scalar_one()
        assert metric.is_suppressed and not metric.analytics_safe


@pytest.mark.asyncio
async def test_operation_replay_concurrent_and_distinct_changes_not_dropped(local_library) -> None:
    maker, actor_id, video_id = local_library
    clip_id = await seed_clip(maker, video_id)
    operation = uuid.uuid4()

    async def submit() -> tuple[uuid.UUID, bool]:
        async with maker() as db:
            actor = await db.get(User, actor_id)
            correction, replayed = await submit_correction(
                db,
                actor=actor,
                clip_id=clip_id,
                correction_type=CorrectionType.formation_tag,
                corrected_value={"formation": "trips"},
                client_operation_id=operation,
            )
            await db.commit()
            return correction.id, replayed

    first, second = await asyncio.gather(submit(), submit())
    assert first[0] == second[0]
    assert sorted([first[1], second[1]]) == [False, True]
    async with maker() as db:
        actor = await db.get(User, actor_id)
        await submit_correction(
            db,
            actor=actor,
            clip_id=clip_id,
            correction_type=CorrectionType.formation_tag,
            corrected_value={"formation": "bunch"},
            client_operation_id=uuid.uuid4(),
        )
        await db.commit()
    async with maker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(CoachCorrection)
                .where(CoachCorrection.clip_id == clip_id)
            )
        ).scalar_one()
        assert count == 2
        assert (await db.get(Clip, clip_id)).label_data["offensive_formation"][
            "formation"
        ] == "bunch"


@pytest.mark.asyncio
async def test_conflicting_revision_and_wrong_identity_target_roll_back(local_library) -> None:
    from app.routers.clips import ClipUpdate, update_clip

    maker, actor_id, video_id = local_library
    clip_id = await seed_clip(maker, video_id)
    other_id = await seed_clip(maker, video_id)
    async with maker() as db:
        tracklet = Tracklet(id=uuid.uuid4(), clip_id=other_id, start_frame=0, end_frame=10)
        db.add(tracklet)
        await db.commit()
        tracklet_id = tracklet.id
    async with maker() as db:
        actor = await db.get(User, actor_id)
        with pytest.raises(HTTPException, match="409"):
            await submit_correction(
                db,
                actor=actor,
                clip_id=clip_id,
                correction_type=CorrectionType.formation_tag,
                corrected_value={"formation": "trips"},
                expected_revision=5,
            )
        await db.rollback()
    async with maker() as db:
        actor = await db.get(User, actor_id)
        with pytest.raises(HTTPException, match="404"):
            await submit_correction(
                db,
                actor=actor,
                clip_id=clip_id,
                correction_type=CorrectionType.player_identity,
                corrected_value={"tracklet_id": str(tracklet_id), "team": "home"},
            )
        await db.rollback()
        assert annotation_revision(await db.get(Clip, clip_id)) == 0
    async with maker() as db:
        actor = await db.get(User, actor_id)
        with pytest.raises(HTTPException, match="422"):
            await update_clip(clip_id, ClipUpdate(end_time=21), db, actor)
        await db.rollback()
        assert (await db.get(Clip, clip_id)).end_time == 8


@pytest.mark.asyncio
async def test_export_writes_actual_local_jsonl_and_replay_exports_nothing(
    local_library, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    maker, actor_id, video_id = local_library
    clip_id = await seed_clip(maker, video_id)
    async with maker() as db:
        actor = await db.get(User, actor_id)
        await submit_correction(
            db,
            actor=actor,
            clip_id=clip_id,
            correction_type=CorrectionType.coverage_tag,
            corrected_value={"coverage": "cover_2"},
        )
        await db.commit()
    async with maker() as db:
        exported = await export_corrections(db, clip_id=clip_id)
        await db.commit()
        from app.models import TrainingDataset

        dataset = await db.get(TrainingDataset, exported.training_dataset_id)
        scheme, bucket, key = parse_storage_uri(dataset.artifact_uri)
        assert scheme == "local"
        records = [
            json.loads(line) for line in local_object_path(bucket, key).read_text().splitlines()
        ]
        assert len(records) == dataset.row_count == 1
        assert records[0]["source"] == "human"
        assert records[0]["label_value"] == {"coverage": "cover_2"}
        assert (await export_corrections(db, clip_id=clip_id)).exported_count == 0


@pytest.mark.asyncio
async def test_actual_holdout_eval_gates_promotion_and_counts_abstentions(
    local_library, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    model_path = local_object_path("artifacts", "models/coverage.json")
    model_path.parent.mkdir(parents=True)
    model_path.write_text('{"model_type":"coverage","provenance":{"train_clip_ids":[]}}')
    maker, actor_id, video_id = local_library
    async with maker() as db:
        actor = await db.get(User, actor_id)
        training_records = []
        training_labels = []
        for category in ("cover_2", "cover_2", "cover_3"):
            training_clip = Clip(
                id=uuid.uuid4(), video_id=video_id, start_time=0, end_time=5, label_data={}
            )
            training_label = Label(
                id=uuid.uuid4(),
                clip_id=training_clip.id,
                label_type="coverage_shell",
                label_value={"shell": category},
                source="human",
                annotated_by=actor.id,
            )
            db.add_all([training_clip, training_label])
            training_labels.append(training_label)
            training_records.append(
                {
                    "label_id": str(training_label.id),
                    "clip_id": str(training_clip.id),
                    "label_type": "coverage_shell",
                    "label_value": {"shell": category},
                    "source": "human",
                }
            )
        training_body = "\n".join(json.dumps(record) for record in training_records) + "\n"
        training_path = local_object_path("artifacts", "datasets/fit.jsonl")
        training_path.parent.mkdir(parents=True, exist_ok=True)
        training_path.write_text(training_body)
        training_dataset = TrainingDataset(
            id=uuid.uuid4(),
            model_scope="coverage",
            source_label_ids=[str(label.id) for label in training_labels],
            source_correction_ids=[],
            row_count=3,
            artifact_uri="local://artifacts/datasets/fit.jsonl",
            changelog={"sha256": hashlib.sha256(training_body.encode()).hexdigest()},
            created_by=actor.id,
        )
        db.add(training_dataset)
        await db.flush()
        model = ModelVersion(
            id=uuid.uuid4(),
            model_name=f"coverage-{uuid.uuid4()}",
            model_type="coverage",
            version="1",
            artifact_uri="local://artifacts/models/coverage.json",
            training_dataset_id=training_dataset.id,
        )
        db.add(model)
        await db.flush()
        truths, predictions = [], []
        for index in range(20):
            clip = Clip(
                id=uuid.uuid4(),
                video_id=video_id,
                start_time=0,
                end_time=5,
                label_data={},
                provenance={"source": "real"},
                is_reviewed=True,
            )
            db.add(clip)
            value = {"shell": "cover_2" if index % 4 else "cover_3"}
            truth = Label(
                id=uuid.uuid4(),
                clip_id=clip.id,
                label_type="coverage_shell",
                label_value=value,
                source="human",
                annotated_by=actor.id,
            )
            predicted = Label(
                id=uuid.uuid4(),
                clip_id=clip.id,
                label_type="coverage_shell",
                label_value={"value": value["shell"], "provenance": {"source": "real"}},
                source="model",
                model_version_id=model.id,
            )
            db.add_all([truth, predicted])
            truths.append(truth.id)
            predictions.append(predicted.id)
        await db.flush()
        with pytest.raises(HTTPException, match="422"):
            await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
        benchmark = await create_benchmark(
            db, label_ids=truths, actor=actor, name="Held-out Toledo coverage"
        )
        # Later coach edits must not change the fitted-label baseline.
        for training_label in training_labels:
            training_label.label_value = {"shell": "cover_3"}
        await db.flush()
        partial = await evaluate_model(
            db,
            model_id=model.id,
            benchmark_id=uuid.UUID(benchmark["id"]),
            prediction_label_ids=predictions[:10],
            actor=actor,
        )
        assert partial["accuracy"] == 0.5 and not partial["passed"]
        measured = await evaluate_model(
            db,
            model_id=model.id,
            benchmark_id=uuid.UUID(benchmark["id"]),
            prediction_label_ids=predictions,
            actor=actor,
        )
        assert measured["accuracy"] == measured["macro_f1"] == 1
        assert measured["baseline_accuracy"] == 0.75
        assert measured["passed"]
        await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
        assert model.promoted_stage == ModelStage.production
        training_path.write_text(training_body + "{}\n")
        with pytest.raises(HTTPException, match="422"):
            await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
        training_path.write_text(training_body)
        await db.commit()


@pytest.mark.asyncio
async def test_training_retry_is_serialized_and_new_work_is_capacity_gated(
    local_library, tmp_path, monkeypatch
) -> None:
    from app.routers.learning import TrainingCreate, train

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKLOAD_GATING_DISABLED", "1")
    get_settings.cache_clear()
    for key in ("labels.jsonl", "features.jsonl", "other-features.jsonl"):
        path = local_object_path("artifacts", key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    maker, actor_id, _video_id = local_library
    async with maker() as db:
        dataset = TrainingDataset(
            id=uuid.uuid4(),
            model_scope="coverage",
            source_label_ids=[],
            source_correction_ids=[],
            row_count=1,
            artifact_uri="local://artifacts/labels.jsonl",
            created_by=actor_id,
        )
        db.add(dataset)
        await db.commit()
        dataset_id = dataset.id
    body = TrainingCreate(
        training_dataset_id=dataset_id, features_uri="local://artifacts/features.jsonl"
    )

    async def enqueue() -> dict[str, str]:
        async with maker() as db:
            actor = await db.get(User, actor_id)
            result = await train(body, db, actor)
            await db.commit()
            return result

    first, second = await asyncio.gather(enqueue(), enqueue())
    assert first["job_id"] == second["job_id"]
    monkeypatch.setenv("WORKLOAD_GATING_DISABLED", "0")
    monkeypatch.setenv("WORKLOAD_QUEUE_THRESHOLD", "0")
    get_settings.cache_clear()
    assert (await enqueue())["job_id"] == first["job_id"]
    async with maker() as db:
        actor = await db.get(User, actor_id)
        with pytest.raises(HTTPException) as rejected:
            await train(
                body.model_copy(update={"features_uri": "local://artifacts/other-features.jsonl"}),
                db,
                actor,
            )
        assert rejected.value.status_code == 503
        assert rejected.value.detail["error_code"] == "workload_gated"
        await db.rollback()


@pytest.mark.asyncio
async def test_metric_policy_and_overlay_use_exact_calibration(local_library) -> None:
    from app.routers.overlays import get_clip_overlays

    maker, actor_id, video_id = local_library
    clip_id = await seed_clip(maker, video_id)
    async with maker() as db:
        actor = await db.get(User, actor_id)
        good = FieldCalibration(
            id=uuid.uuid4(),
            video_id=video_id,
            analytics_safe=True,
            confidence=0.95,
            provenance={"source": "manual"},
        )
        bad = FieldCalibration(
            id=uuid.uuid4(), video_id=video_id, analytics_safe=False, confidence=0.1
        )
        db.add_all([good, bad])
        await db.flush()
        clip = await db.get(Clip, clip_id)
        clip.calibration_version_id = good.id
        values = [
            Metric(
                id=uuid.uuid4(),
                clip_id=clip_id,
                metric_name="speed",
                metric_value={},
                analytics_safe=True,
                provenance={"source": "real", "validated": True},
            ),
            Metric(
                id=uuid.uuid4(),
                clip_id=clip_id,
                metric_name="fake",
                metric_value={},
                analytics_safe=True,
                provenance={"source": "synthetic_test"},
            ),
            Metric(
                id=uuid.uuid4(),
                clip_id=clip_id,
                metric_name="workload_fusion",
                metric_value={},
                analytics_safe=True,
                provenance={"source": "real"},
            ),
            Metric(
                id=uuid.uuid4(),
                clip_id=clip_id,
                metric_name="candidate",
                metric_value={},
                analytics_safe=False,
                experimental_flag=True,
                provenance={"source": "real", "validated": False},
            ),
        ]
        db.add_all(values)
        await db.commit()
        visible = (
            (
                await db.execute(
                    metric_visibility(select(Metric).where(Metric.clip_id == clip_id), actor)
                )
            )
            .scalars()
            .all()
        )
        assert [value.metric_name for value in visible] == ["speed"]
        overlay = await get_clip_overlays(clip_id=clip_id, db=db, current_user=actor)
        assert overlay.calibration.calibration_id == good.id
        assert overlay.calibration.analytics_safe
        assert [value.metric_name for value in overlay.metrics] == ["speed"]
