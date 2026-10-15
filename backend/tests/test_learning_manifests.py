"""Manifest wiring and split isolation with structural graph fixtures, not accuracy evidence."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import (
    Clip,
    FootballSession,
    JobStatus,
    JobType,
    Label,
    Play,
    PlayView,
    ProcessingJob,
    SystemSetting,
    TrainingDataset,
    User,
    UserRole,
    Video,
)
from app.services.learning_manifests import (
    MAX_FILE_BYTES,
    _coverage_class,
    _read_packets,
    _valid_graph,
    create_manifest,
    preview_manifest,
)
from app.storage import ARTIFACTS, local_object_path, parse_storage_uri
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _packet(clip_id):
    return {
        "clip_id": str(clip_id),
        "source": "real",
        "analytics_safe": True,
        "graph": {
            "schema_version": 1,
            "node_features": [[float(index), 2.0, 3.0, 4.0, 5.0, 6.0, 7.0] for index in range(5)],
            "edge_index": [[0, 1], [1, 0]],
            "edge_features": [[1.0, 2.0, 3.0, 0.5], [1.0, -2.0, -3.0, 0.5]],
        },
    }


def _save_packets(key, packets):
    path = local_object_path(ARTIFACTS, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(packet) for packet in packets) + "\n")
    return f"local://{ARTIFACTS}/{key}"


@pytest_asyncio.fixture
async def manifest_db(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    get_settings.cache_clear()
    schema = f"manifest_test_{uuid.uuid4().hex}"
    admin = create_async_engine(get_settings().database_url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        get_settings().database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: Base.metadata.create_all(c, checkfirst=False))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            actor = User(
                id=uuid.uuid4(),
                email="manifest@test.local",
                hashed_password="x",
                full_name="Manifest tester",
                role=UserRole.admin,
            )
            db.add(actor)
            await db.flush()
            assignments = []
            data = {}
            for split, count in (("train", 30), ("calibration", 10), ("holdout", 20)):
                session = FootballSession(
                    id=uuid.uuid4(), title=f"Manifest {split}", created_by=actor.id
                )
                db.add(session)
                await db.flush()
                video = Video(
                    id=uuid.uuid4(),
                    filename=f"{split}.mp4",
                    storage_uri=f"local://raw-video/{split}.mp4",
                    practice_session_id=session.id,
                )
                db.add(video)
                await db.flush()
                clips, labels, packets = [], [], []
                for index in range(count):
                    clip = Clip(
                        id=uuid.uuid4(),
                        video_id=video.id,
                        start_time=index * 5,
                        end_time=index * 5 + 4,
                        is_reviewed=True,
                        provenance={"source": "real"},
                    )
                    db.add(clip)
                    await db.flush()
                    label = Label(
                        id=uuid.uuid4(),
                        clip_id=clip.id,
                        label_type="coverage_shell",
                        label_value={"shell": "cover_1" if index % 2 == 0 else "cover_3"},
                        source="human",
                        annotated_by=actor.id,
                    )
                    db.add(label)
                    clips.append(clip)
                    labels.append(label)
                    packets.append(_packet(clip.id))
                uri = _save_packets(f"source/{split}.jsonl", packets)
                job = ProcessingJob(
                    id=uuid.uuid4(),
                    video_id=video.id,
                    job_type=JobType.pipeline,
                    status=JobStatus.succeeded,
                    finished_at=datetime.now(UTC),
                    output_artifacts={"feature_artifact_uri": uri},
                )
                db.add(job)
                assignments.append({"session_id": str(session.id), "split": split})
                data[split] = {
                    "session": session,
                    "video": video,
                    "clips": clips,
                    "labels": labels,
                    "packets": packets,
                    "job": job,
                }
            await db.flush()
            yield db, actor, assignments, data
            await db.rollback()
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def test_manifest_materializes_disjoint_files_and_train_only_dataset(manifest_db):
    db, actor, assignments, data = manifest_db
    preview = await preview_manifest(db, assignments, "Coverage snapshot")
    assert preview["ready"] and preview["counts"] == {"train": 30, "calibration": 10, "holdout": 20}
    result = await create_manifest(db, assignments, "Coverage snapshot", actor)

    def rows(uri):
        _, bucket, key = parse_storage_uri(uri)
        return [
            json.loads(line) for line in local_object_path(bucket, key).read_text().splitlines()
        ]

    features, labels, train = (
        rows(result["features_uri"]),
        rows(result["labels_uri"]),
        rows(result["training_labels_uri"]),
    )
    assert len(features) == len(labels) == 60 and len(train) == 30
    assert {row["split"] for row in train} == {"train"}
    assert {row["session_id"] for row in features if row["split"] == "holdout"} == {
        assignments[2]["session_id"]
    }
    dataset = await db.get(TrainingDataset, uuid.UUID(result["training_dataset_id"]))
    assert dataset.row_count == 30 and set(dataset.source_label_ids) == {
        str(label.id) for label in data["train"]["labels"]
    }
    assert dataset.artifact_uri == result["training_labels_uri"]
    assert dataset.changelog["combined_labels_uri"] == result["labels_uri"]
    assert len(dataset.changelog["calibration_label_ids"]) == 10
    assert len(dataset.changelog["holdout_label_ids"]) == 20
    record = await db.get(SystemSetting, f"learning:manifest:{result['id']}")
    assert record.value["hashes"] == result["hashes"]
    benchmark = await db.get(SystemSetting, f"learning:benchmark:{result['benchmark_id']}")
    assert len(benchmark.value["samples"]) == 20
    assert all(clip.label_data["_learning_split"] == "holdout" for clip in data["holdout"]["clips"])
    assert all(
        clip.label_data["_learning_split"] == "calibration" for clip in data["calibration"]["clips"]
    )
    attempted = [
        {**item, "split": "train" if item["split"] == "holdout" else item["split"]}
        for item in assignments
    ]
    protected = await preview_manifest(db, attempted)
    assert not protected["ready"] and any(
        detail["code"] == "protected_split" for detail in protected["issue_details"]
    )
    with pytest.raises(HTTPException) as blocked:
        await create_manifest(db, attempted, "Leaking split", actor)
    assert blocked.value.status_code == 422


async def test_latest_completed_job_has_no_fallback_to_older_graphs(manifest_db):
    db, actor, assignments, data = manifest_db
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=data["train"]["video"].id,
        job_type=JobType.pipeline,
        status=JobStatus.succeeded,
        finished_at=datetime.now(UTC) + timedelta(seconds=10),
        output_artifacts={"result_state": "partial"},
    )
    db.add(job)
    await db.flush()
    preview = await preview_manifest(db, assignments)
    assert not preview["ready"] and preview["counts"]["train"] == 0
    assert (
        str(job.id) in preview["job_ids"] and str(data["train"]["job"].id) not in preview["job_ids"]
    )
    assert any(detail["code"] == "missing_features" for detail in preview["issue_details"])


async def test_matched_views_count_once_and_conflicting_truth_excludes_play(manifest_db):
    db, actor, assignments, data = manifest_db
    train = data["train"]
    original = train["clips"][0]
    play = Play(id=uuid.uuid4(), session_id=train["session"].id)
    video = Video(
        id=uuid.uuid4(),
        filename="second-view.mp4",
        storage_uri="local://raw-video/second-view.mp4",
        practice_session_id=train["session"].id,
    )
    db.add_all([play, video])
    await db.flush()
    other = Clip(
        id=uuid.uuid4(),
        video_id=video.id,
        start_time=8,
        end_time=12,
        is_reviewed=True,
        provenance={"source": "real"},
    )
    db.add(other)
    await db.flush()
    db.add_all(
        [
            PlayView(play_id=play.id, clip_id=original.id),
            PlayView(play_id=play.id, clip_id=other.id),
        ]
    )
    truth = Label(
        id=uuid.uuid4(),
        clip_id=other.id,
        label_type="coverage_tag",
        label_value={"label": "Cover 1"},
        source="human",
        annotated_by=actor.id,
    )
    db.add(truth)
    uri = _save_packets("source/second-view.jsonl", [_packet(other.id)])
    db.add(
        ProcessingJob(
            video_id=video.id,
            job_type=JobType.pipeline,
            status=JobStatus.succeeded,
            finished_at=datetime.now(UTC),
            output_artifacts={"feature_artifact_uri": uri},
        )
    )
    await db.flush()
    preview = await preview_manifest(db, assignments)
    assert preview["ready"] and preview["counts"]["train"] == 30
    truth.label_value = {"coverage": "cover_4"}
    await db.flush()
    conflict = await preview_manifest(db, assignments)
    assert not conflict["ready"] and conflict["counts"]["train"] == 29
    assert any(detail["code"] == "conflicting_views" for detail in conflict["issue_details"])


async def test_unreviewed_synthetic_ambiguous_or_stale_labels_cannot_fill_counts(manifest_db):
    db, actor, assignments, data = manifest_db
    train = data["train"]
    train["clips"][0].is_reviewed = False
    train["packets"][1]["source"] = "synthetic_test"
    train["packets"][2]["analytics_safe"] = False
    train["packets"][3]["graph"]["edge_index"] = [[0], [99]]
    train["packets"][3]["graph"]["edge_features"] = [[1.0, 2.0, 3.0, 4.0]]
    # An invalid latest annotation must not silently fall back to the prior valid label.
    db.add(
        Label(
            clip_id=train["clips"][4].id,
            label_type="coverage_tag",
            label_value={"coverage": "cover_2"},
            source="human",
            annotated_by=actor.id,
            created_at=datetime.now(UTC) + timedelta(seconds=10),
        )
    )
    _save_packets("source/train.jsonl", train["packets"])
    await db.flush()
    preview = await preview_manifest(db, assignments)
    assert not preview["ready"] and preview["counts"]["train"] == 25
    assert {detail["code"] for detail in preview["issue_details"]} >= {
        "invalid_truth",
        "invalid_graph",
        "insufficient_split",
    }


async def test_session_leakage_cannot_create_manifest(manifest_db):
    db, actor, assignments, data = manifest_db
    preview = await preview_manifest(
        db, assignments + [{"session_id": assignments[0]["session_id"], "split": "holdout"}]
    )
    assert not preview["ready"] and any(
        detail["code"] == "session_leakage" for detail in preview["issue_details"]
    )


def test_graph_validation_and_bounded_local_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    assert _coverage_class({"label": "Cover-2 MOF"}) == "cover_2_mof"
    assert _coverage_class({"coverage": "cover_2"}) is None
    assert _coverage_class({"shell": "cover_1", "coverage": "cover_3"}) is None
    packet = _packet(uuid.uuid4())
    assert _valid_graph(packet)
    packet["graph"]["node_features"][0][0] = float("nan")
    assert not _valid_graph(packet)
    uri = _save_packets("limits/rows.jsonl", [_packet(uuid.uuid4()), _packet(uuid.uuid4())])
    with pytest.raises(ValueError, match="10000-row"):
        _read_packets(uri, MAX_FILE_BYTES, 1)
    _, bucket, key = parse_storage_uri(uri)
    path = local_object_path(bucket, key)
    with path.open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="16 MiB"):
        _read_packets(uri, MAX_FILE_BYTES * 2, 10000)
    path.write_text('{"clip_id":"x","confidence":NaN}\n')
    with pytest.raises(ValueError, match="invalid JSON constant"):
        _read_packets(uri, MAX_FILE_BYTES, 10000)


async def test_corrected_observations_and_stale_bounds_require_reprocessing(manifest_db):
    db, actor, assignments, data = manifest_db
    train = data["train"]
    edited, mismatched = train["clips"][:2]
    # The coach reviewed this boundary edit, but its old graph has no revision lineage.
    edited.start_time += 0.5
    edited.label_data = {"_observation_revision": 1}
    edited.is_reviewed = True
    # Explicit bounds must match even an otherwise unedited legacy clip.
    stale = train["packets"][1]
    stale["source_start_seconds"] = mismatched.start_time
    stale["source_end_seconds"] = mismatched.end_time + 1
    _save_packets("source/train.jsonl", train["packets"])
    await db.flush()
    preview = await preview_manifest(db, assignments)
    assert not preview["ready"] and preview["counts"]["train"] == 28
    details = [detail for detail in preview["issue_details"] if detail["code"] == "stale_features"]
    assert details and details[0]["count"] == 2
    for clip, packet in zip(train["clips"][:2], train["packets"][:2], strict=True):
        packet.update(
            source_start_seconds=clip.start_time,
            source_end_seconds=clip.end_time,
            source_observation_revision=(clip.label_data or {}).get("_observation_revision", 0),
        )
    _save_packets("source/train.jsonl", train["packets"])
    current = await preview_manifest(db, assignments)
    assert current["ready"] and current["counts"]["train"] == 30
    # Matching bounds alone cannot conceal a stale identity/event observation revision.
    edited.label_data = {"_observation_revision": 2}
    await db.flush()
    stale_revision = await preview_manifest(db, assignments)
    assert not stale_revision["ready"] and stale_revision["counts"]["train"] == 29
