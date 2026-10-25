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


@pytest.mark.asyncio
async def test_staff_referee_classification_is_reversible_and_cannot_receive_player_identity(
    local_library,
):
    maker, actor_id, video_id = local_library
    async with maker() as db:
        actor = await db.get(User, actor_id)
        clip = Clip(id=uuid.uuid4(), video_id=video_id, start_time=0, end_time=1)
        db.add(clip)
        await db.flush()
        track = Tracklet(
            id=uuid.uuid4(),
            clip_id=clip.id,
            start_frame=0,
            end_frame=1,
            team_label="unknown",
            provenance={"source": "real", "actor_kind": "player"},
        )
        db.add(track)
        await db.flush()
        await submit_correction(
            db,
            actor=actor,
            clip_id=clip.id,
            correction_type=CorrectionType.player_identity,
            corrected_value={"tracklet_id": str(track.id), "actor_kind": "official"},
        )
        assert track.player_id is None and track.team_label == "official"
        assert track.provenance["actor_classification"]["method"] == "staff_review"
        await db.commit()
        from app.routers.tracklets import TrackletUpdate, update_tracklet

        await update_tracklet(
            track.id,
            TrackletUpdate(
                provenance={
                    "actor_classification": {
                        "method": "detection_official_candidate",
                        "validated": False,
                    },
                }
            ),
            db,
            actor,
        )
        assert track.provenance["actor_classification"]["method"] == "staff_review"
        with pytest.raises(HTTPException) as rejected:
            await update_tracklet(
                track.id, TrackletUpdate(provenance={"actor_kind": "player"}), db, actor
            )
        assert rejected.value.status_code == 422
        with pytest.raises(HTTPException) as error:
            await submit_correction(
                db,
                actor=actor,
                clip_id=clip.id,
                correction_type=CorrectionType.player_identity,
                corrected_value={"tracklet_id": str(track.id), "jersey_number": 7},
            )
        assert error.value.status_code == 422
        await submit_correction(
            db,
            actor=actor,
            clip_id=clip.id,
            correction_type=CorrectionType.player_identity,
            corrected_value={
                "tracklet_id": str(track.id),
                "actor_kind": "player",
                "team": "defense",
            },
        )
        assert track.team_label == "defense" and track.provenance["actor_kind"] == "player"
        await db.commit()


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
    model_path.write_text(
        '{"schema_version":1,"model_type":"coverage","provenance":{"train_clip_ids":[]}}'
    )
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
        training_path.write_bytes(training_body.encode("utf-8"))
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
        truths, predictions, prediction_rows = [], [], []
        for index in range(20):
            clip = Clip(
                id=uuid.uuid4(),
                video_id=video_id,
                start_time=0,
                end_time=5,
                label_data={},
                provenance={"source": "real"},
                capture_regime="fixed_sideline",
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
            db.add(truth)
            truths.append(truth.id)
            predictions.append(predicted.id)
            prediction_rows.append(predicted)
        await db.flush()
        with pytest.raises(HTTPException, match="422"):
            await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
        benchmark = await create_benchmark(
            db, label_ids=truths, actor=actor, name="Held-out Toledo coverage"
        )
        from label_producer_helpers import publish_pipeline_predictions

        video = await db.get(Video, video_id)
        await publish_pipeline_predictions(db, actor, video, model, prediction_rows, monkeypatch)
        model.training_dataset_id = None
        with pytest.raises(HTTPException, match="frozen training snapshot baseline"):
            await evaluate_model(
                db,
                model_id=model.id,
                benchmark_id=uuid.UUID(benchmark["id"]),
                prediction_label_ids=predictions,
                actor=actor,
            )
        model.training_dataset_id = training_dataset.id
        original_changelog = training_dataset.changelog
        training_dataset.changelog = {}
        with pytest.raises(HTTPException, match="hash-bound frozen training snapshot"):
            await evaluate_model(
                db,
                model_id=model.id,
                benchmark_id=uuid.UUID(benchmark["id"]),
                prediction_label_ids=predictions,
                actor=actor,
            )
        training_dataset.changelog = original_changelog
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
        assert "__abstained__" in partial["confusion_matrix"]["labels"]
        measured = await evaluate_model(
            db,
            model_id=model.id,
            benchmark_id=uuid.UUID(benchmark["id"]),
            prediction_label_ids=predictions,
            actor=actor,
        )
        assert measured["accuracy"] == measured["macro_f1"] == 1
        assert measured["baseline_accuracy"] == 0.75
        assert sum(map(sum, measured["confusion_matrix"]["counts"])) == 20
        stored = await db.get(SystemSetting, f"learning:evaluation:{measured['id']}")
        assert stored.value["confusion_matrix"] == measured["confusion_matrix"]
        assert measured["passed"]
        assert measured["capture_regimes"] == ["fixed_sideline"]
        assert measured["label_scope"]["slices"]["fixed_sideline"]["baseline_accuracy"] == 0.75
        assert measured["baseline"]["definition"] == "training_population_majority_class"
        assert (
            measured["baseline"]["training_snapshot_sha256"]
            == hashlib.sha256(training_body.encode()).hexdigest()
        )
        pristine = dict(stored.value)
        for forged in [
            {**pristine, "baseline_policy_version": None, "baseline_accuracy": None},
            {**pristine, "baseline_accuracy": 0.0},
            {**pristine, "capture_regimes": ["drone_follow"]},
            {**pristine, "label_scope": {**pristine["label_scope"], "feature_schema_version": 2}},
            {**pristine, "label_scope": {**pristine["label_scope"], "passed": False}},
            {**pristine, "baseline": {**pristine["baseline"], "majority_class": "forged"}},
        ]:
            stored.value = forged
            with pytest.raises(HTTPException, match="422"):
                await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
            assert model.promoted_stage != ModelStage.production
        stored.value = pristine
        await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
        assert model.promoted_stage == ModelStage.production
        approved_sha = model.metrics["approved_artifact_sha256"]
        assert approved_sha == measured["artifact_sha256"]
        approved_scope = model.metrics["label_publication_approvals"]["coverage_shell"]
        assert approved_scope["capture_regimes"] == ["fixed_sideline"]
        assert approved_scope["feature_schema_version"] == 1
        assert approved_scope["artifact_sha256"] == approved_sha
        assert approved_scope["evaluation_id"] == measured["id"]
        from label_producer_helpers import evaluate_and_promote

        await evaluate_and_promote(db, actor, model, benchmark["id"], predictions)
        model.training_dataset_id = None
        with pytest.raises(HTTPException, match="frozen training snapshot baseline"):
            await evaluate_model(
                db,
                model_id=model.id,
                benchmark_id=uuid.UUID(benchmark["id"]),
                prediction_label_ids=predictions,
                actor=actor,
            )
        assert model.promoted_stage == ModelStage.production
        assert model.metrics["approved_artifact_sha256"] == approved_sha
        model.training_dataset_id = training_dataset.id
        training_path.write_bytes((training_body + "{}\n").encode("utf-8"))
        with pytest.raises(HTTPException, match="422"):
            await promote_model(db, model=model, stage=ModelStage.production, actor=actor)
        training_path.write_bytes(training_body.encode("utf-8"))
        await evaluate_model(
            db,
            model_id=model.id,
            benchmark_id=uuid.UUID(benchmark["id"]),
            prediction_label_ids=predictions[:10],
            actor=actor,
        )
        # A later failed evaluation never moves the human-approved serving binding.
        assert model.metrics["approved_artifact_sha256"] == approved_sha
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
