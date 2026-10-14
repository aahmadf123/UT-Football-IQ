"""Materialize session-disjoint coverage learning snapshots from real saved graphs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Clip,
    FootballSession,
    JobStatus,
    Label,
    Play,
    PlayView,
    ProcessingJob,
    SystemSetting,
    TrainingDataset,
    User,
    Video,
)
from app.services.learning import create_benchmark
from app.storage import ARTIFACTS, is_valid_key, local_object_path, parse_storage_uri

MIN_COUNTS = {"train": 30, "calibration": 10, "holdout": 20}
COVERAGE_CLASSES = frozenset(
    {
        "cover_0",
        "cover_1",
        "cover_2_mof",
        "cover_2_shell",
        "cover_3",
        "cover_4",
        "man_free",
        "bracket_match",
        "cover_6",
    }
)
MAX_JOBS = 100
MAX_ROWS = 10000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MANIFEST_PREFIX = "learning:manifest:"


@dataclass
class _Collection:
    sessions: list[dict[str, str]] = field(default_factory=list)
    clips: dict[uuid.UUID, Clip] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)
    group_clips: dict[str, list[Clip]] = field(default_factory=dict)
    jobs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    artifact_hashes: dict[str, str] = field(default_factory=dict)

    def issue(self, code: str, message: str, *, blocking: bool = False, **detail: Any) -> None:
        for previous in self.details:
            if previous["code"] == code and previous["message"] == message:
                previous["count"] = previous.get("count", 1) + detail.get("count", 1)
                self.blocked |= blocking
                return
        self.issues.append(message)
        self.details.append({"code": code, "message": message, "blocking": blocking, **detail})
        self.blocked |= blocking

    def preview(self) -> dict[str, Any]:
        counts = {
            split: sum(row["split"] == split for row in self.examples) for split in MIN_COUNTS
        }
        classes = {
            split: dict(
                sorted(
                    Counter(row["truth"] for row in self.examples if row["split"] == split).items()
                )
            )
            for split in MIN_COUNTS
        }
        issues = list(self.issues)
        details = list(self.details)
        valid = not self.blocked
        for split, minimum in MIN_COUNTS.items():
            if counts[split] < minimum:
                valid = False
                message = (
                    f"{split} has {counts[split]} eligible plays; at least {minimum} are required."
                )
                issues.append(message)
                details.append(
                    {
                        "code": "insufficient_split",
                        "split": split,
                        "message": message,
                        "blocking": True,
                    }
                )
        train = classes["train"]
        if len(train) < 2 or any(count < 5 for count in train.values()):
            valid = False
            message = (
                "Training needs at least two coverage classes "
                "and five plays in every included class."
            )
            issues.append(message)
            details.append(
                {"code": "insufficient_train_classes", "message": message, "blocking": True}
            )
        for split in ("calibration", "holdout"):
            split_classes = classes[split]
            if (
                len(split_classes) < 2
                or any(count < 2 for count in split_classes.values())
                or not split_classes.keys() <= train.keys()
            ):
                valid = False
                message = (
                    f"{split} needs at least two training-supported classes "
                    "and two plays per class."
                )
                issues.append(message)
                details.append(
                    {
                        "code": "insufficient_validation_classes",
                        "split": split,
                        "message": message,
                        "blocking": True,
                    }
                )
        return {
            "ready": valid,
            "counts": counts,
            "required": dict(MIN_COUNTS),
            "class_counts": classes,
            "issues": issues,
            "issue_details": details,
            "job_ids": self.jobs,
            "sessions": self.sessions,
            "eligible_play_count": len(self.examples),
        }


def _coverage_class(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    answers = set()
    for key in ("shell", "coverage", "label", "coverage_shell"):
        raw = value.get(key)
        if raw is not None:
            if not isinstance(raw, str):
                return None
            answer = "_".join(raw.strip().lower().replace("-", " ").replace("_", " ").split())
            answers.add(answer)
    return next(iter(answers)) if len(answers) == 1 and answers <= COVERAGE_CLASSES else None


def _finite_number(value: Any) -> bool:
    try:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        return False


def _finite_row(row: Any, size: int) -> bool:
    return (
        isinstance(row, list) and len(row) == size and all(_finite_number(value) for value in row)
    )


def _parse_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Feature artifact contains non-finite numbers")
    return value


def _reject_json_constant(raw: str) -> None:
    raise ValueError(f"Feature artifact contains invalid JSON constant {raw}")


def _observations_current(packet: dict[str, Any], clip: Clip) -> bool:
    """Legacy unedited graphs are usable; changed observations need exact lineage."""
    data = clip.label_data if isinstance(clip.label_data, dict) else {}
    revision = data.get("_observation_revision", 0)
    if type(revision) is not int or revision < 0:
        return False
    revision_present = "source_observation_revision" in packet
    if revision_present and (
        type(packet["source_observation_revision"]) is not int
        or packet["source_observation_revision"] != revision
    ):
        return False
    bound_keys = ("source_start_seconds", "source_end_seconds")
    bounds_present = [key in packet for key in bound_keys]
    if any(bounds_present):
        if not all(bounds_present):
            return False
        if not all(_finite_number(packet[key]) for key in bound_keys):
            return False
        if packet[bound_keys[0]] != clip.start_time or packet[bound_keys[1]] != clip.end_time:
            return False
    return revision == 0 or (revision_present and all(bounds_present))


def _valid_graph(packet: dict[str, Any]) -> bool:
    if packet.get("source") != "real" or packet.get("analytics_safe") is not True:
        return False
    graph = packet.get("graph")
    if (
        not isinstance(graph, dict)
        or type(graph.get("schema_version")) is not int
        or graph["schema_version"] != 1
    ):
        return False
    nodes, edges, features = (
        graph.get("node_features"),
        graph.get("edge_index"),
        graph.get("edge_features"),
    )
    if (
        not isinstance(nodes, list)
        or len(nodes) < 5
        or not all(_finite_row(row, 7) for row in nodes)
    ):
        return False
    if (
        not isinstance(edges, list)
        or len(edges) != 2
        or not all(isinstance(row, list) for row in edges)
        or len(edges[0]) != len(edges[1])
    ):
        return False
    if (
        not isinstance(features, list)
        or len(features) != len(edges[0])
        or not all(_finite_row(row, 4) for row in features)
    ):
        return False
    return all(type(index) is int and 0 <= index < len(nodes) for row in edges for index in row)


def _read_packets(
    uri: str, remaining_bytes: int, remaining_rows: int
) -> tuple[list[dict[str, Any]], int, str]:
    scheme, bucket, key = parse_storage_uri(uri)
    if scheme != "local" or bucket != ARTIFACTS or not is_valid_key(key):
        raise ValueError("Feature graphs must be imported into local artifact storage.")
    path = local_object_path(bucket, key)
    size = path.stat().st_size
    if not path.is_file() or size > MAX_FILE_BYTES or size > remaining_bytes:
        raise ValueError("Feature artifacts exceed the 16 MiB per-file or 64 MiB total limit.")
    # Cap the actual read too: a concurrent replacement or growth cannot bypass stat.
    with path.open("rb") as stream:
        raw = stream.read(min(MAX_FILE_BYTES, remaining_bytes) + 1)
    if len(raw) > MAX_FILE_BYTES or len(raw) > remaining_bytes:
        raise ValueError("Feature artifact grew beyond the permitted size.")
    packets: list[dict[str, Any]] = []
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            if len(packets) >= remaining_rows:
                raise ValueError("Feature artifacts exceed the 10000-row limit.")
            packet = json.loads(
                line, parse_float=_parse_float, parse_constant=_reject_json_constant
            )
            if not isinstance(packet, dict):
                raise ValueError("Feature JSONL rows must be objects.")
            packets.append(packet)
    return packets, len(raw), hashlib.sha256(raw).hexdigest()


async def _collect(
    db: AsyncSession, sessions: list[dict[str, Any]], *, lock: bool = False
) -> _Collection:
    result = _Collection()
    assignments: dict[uuid.UUID, str] = {}
    if not sessions or len(sessions) > MAX_JOBS:
        result.issue(
            "session_selection",
            "Choose between one and 100 sessions with explicit splits.",
            blocking=True,
        )
        return result
    for item in sessions:
        try:
            session_id = uuid.UUID(str(item["session_id"]))
            split = item["split"]
            if split not in MIN_COUNTS:
                raise ValueError("Invalid split")
        except (ValueError, TypeError, KeyError):
            result.issue(
                "invalid_session_assignment",
                ("Each session needs a valid UUID and train, calibration, or holdout split."),
                blocking=True,
            )
            continue
        if session_id in assignments and assignments[session_id] != split:
            result.issue(
                "session_leakage",
                "Recordings from one session cannot cross learning splits.",
                blocking=True,
                session_id=str(session_id),
            )
        assignments[session_id] = split
    result.sessions = [
        {"session_id": str(sid), "split": split}
        for sid, split in sorted(assignments.items(), key=lambda pair: str(pair[0]))
    ]
    if not assignments:
        return result
    stmt = (
        select(FootballSession)
        .where(FootballSession.id.in_(assignments))
        .order_by(FootballSession.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    selected = (await db.execute(stmt)).scalars().all()
    if len(selected) != len(assignments):
        result.issue(
            "missing_session", "One or more selected sessions no longer exist.", blocking=True
        )
    videos = (
        (await db.execute(select(Video).where(Video.practice_session_id.in_(assignments))))
        .scalars()
        .all()
    )
    by_video = {video.id: video for video in videos}
    clip_stmt = (
        select(Clip, PlayView.play_id, Play.session_id)
        .outerjoin(PlayView, PlayView.clip_id == Clip.id)
        .outerjoin(Play, Play.id == PlayView.play_id)
        .where(Clip.video_id.in_(by_video))
        .order_by(Clip.id)
        .limit(MAX_ROWS + 1)
    )
    if lock:
        clip_stmt = clip_stmt.with_for_update(of=Clip)
    clip_rows = (await db.execute(clip_stmt)).all()
    if len(clip_rows) > MAX_ROWS:
        result.issue(
            "clip_limit",
            "Selected sessions exceed the 10000-clip limit; choose fewer sessions.",
            blocking=True,
        )
        return result
    targets: dict[uuid.UUID, tuple[str, str, str]] = {}
    for clip, play_id, play_session_id in clip_rows:
        video = by_video[clip.video_id]
        sid = video.practice_session_id
        if sid is None:
            result.issue(
                "missing_session",
                "Recording session association changed; retry selection.",
                blocking=True,
            )
            continue
        split = assignments[sid]
        result.clips[clip.id] = clip
        group = str(play_id or clip.id)
        result.group_clips.setdefault(group, []).append(clip)
        targets[clip.id] = (group, str(sid), split)
        data = clip.label_data if isinstance(clip.label_data, dict) else {}
        protected = data.get("_learning_split")
        if (protected == "holdout" and split != "holdout") or (
            protected == "calibration" and split == "train"
        ):
            result.issue(
                "protected_split",
                ("Protected holdout or calibration film cannot be moved into model fitting data."),
                blocking=True,
                clip_id=str(clip.id),
            )
        if play_id and play_session_id != sid:
            result.issue(
                "play_session_mismatch",
                (
                    "A canonical play belongs to a different session; "
                    "repair its recording grouping first."
                ),
                blocking=True,
                clip_id=str(clip.id),
            )
    ranked = (
        select(
            Label.id.label("id"),
            func.row_number()
            .over(partition_by=Label.clip_id, order_by=(Label.created_at.desc(), Label.id.desc()))
            .label("rank"),
        )
        .where(
            Label.clip_id.in_(result.clips),
            Label.tracklet_id.is_(None),
            Label.source == "human",
            Label.label_type.in_(["coverage_shell", "coverage_tag"]),
        )
        .subquery()
    )
    labels = (
        (
            await db.execute(
                select(Label).join(ranked, ranked.c.id == Label.id).where(ranked.c.rank == 1)
            )
        )
        .scalars()
        .all()
    )
    by_clip = {label.clip_id: label for label in labels if label.clip_id is not None}
    truth: dict[uuid.UUID, tuple[Label, str]] = {}
    invalid_groups = set()
    group_classes: dict[str, set[str]] = defaultdict(set)
    for clip_id, label in by_clip.items():
        clip = result.clips[clip_id]
        if not clip.is_reviewed:
            continue
        group = targets[clip_id][0]
        category = _coverage_class(label.label_value)
        source = (
            (clip.provenance or {}).get("source") if isinstance(clip.provenance, dict) else None
        )
        if (
            category is None
            or label.annotated_by is None
            or source in {"synthetic", "synthetic_test", "stub"}
        ):
            invalid_groups.add(group)
            result.issue(
                "invalid_truth",
                (
                    "Reviewed coverage truth must be attributed to staff "
                    "and name one supported class; "
                    "ambiguous Cover 2 and synthetic film are excluded."
                ),
                clip_id=str(clip_id),
            )
            continue
        truth[clip_id] = (label, category)
        group_classes[group].add(category)
    for group, categories in group_classes.items():
        if len(categories) > 1:
            invalid_groups.add(group)
            result.issue(
                "conflicting_views",
                (
                    "Matched camera views have conflicting reviewed coverage labels; "
                    "this play is excluded."
                ),
                canonical_play_id=group,
            )
    jobs = (
        (
            await db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.video_id.in_(by_video),
                    ProcessingJob.status == JobStatus.succeeded,
                )
                .distinct(ProcessingJob.video_id)
                .order_by(
                    ProcessingJob.video_id,
                    ProcessingJob.finished_at.desc().nullslast(),
                    ProcessingJob.created_at.desc(),
                    ProcessingJob.id.desc(),
                )
                .limit(MAX_JOBS + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(jobs) > MAX_JOBS:
        result.issue(
            "job_limit",
            "Selected sessions exceed the 100-recording processing-job limit.",
            blocking=True,
        )
        return result
    result.jobs = [str(job.id) for job in jobs]
    seen_videos = {job.video_id for job in jobs}
    if len(seen_videos) != len(by_video):
        result.issue(
            "missing_processing",
            (
                "Some recordings have no completed processing job; "
                "process them with the detailed profile to collect coverage graphs."
            ),
            count=len(by_video) - len(seen_videos),
        )
    byte_count = row_count = 0
    chosen: dict[str, dict[str, Any]] = {}
    for job in jobs:
        uri = (job.output_artifacts or {}).get("feature_artifact_uri")
        if not isinstance(uri, str):
            result.issue(
                "missing_features",
                (
                    "The latest completed job has no saved coverage graphs; "
                    "run detailed processing on that recording."
                ),
                job_id=str(job.id),
            )
            continue
        try:
            packets, bytes_read, digest = await to_thread.run_sync(
                _read_packets, uri, MAX_TOTAL_BYTES - byte_count, MAX_ROWS - row_count
            )
        except (ValueError, TypeError, OSError, UnicodeError) as exc:
            result.issue(
                "invalid_feature_artifact",
                "A saved feature artifact is missing, malformed, or exceeds processing bounds.",
                blocking=True,
                job_id=str(job.id),
                reason=str(exc),
            )
            continue
        byte_count += bytes_read
        row_count += len(packets)
        result.artifact_hashes[str(job.id)] = digest
        packet_ids = set()
        for packet in packets:
            try:
                clip_id = uuid.UUID(str(packet.get("clip_id", "")))
            except ValueError:
                result.issue(
                    "invalid_feature_target",
                    "A feature row has no valid clip ID and is excluded.",
                    job_id=str(job.id),
                )
                continue
            if clip_id in packet_ids:
                result.issue(
                    "duplicate_feature_target",
                    "A saved feature artifact repeats a clip target and cannot be used.",
                    blocking=True,
                    job_id=str(job.id),
                    clip_id=str(clip_id),
                )
                continue
            packet_ids.add(clip_id)
            clip = result.clips.get(clip_id)
            if clip is None or clip.video_id != job.video_id:
                result.issue(
                    "invalid_feature_target",
                    "A feature row does not belong to its processed recording and is excluded.",
                    job_id=str(job.id),
                )
                continue
            group, packet_session_id, split = targets[clip_id]
            if group in invalid_groups or clip_id not in truth:
                continue
            if not _observations_current(packet, clip):
                result.issue(
                    "stale_features",
                    (
                        "Saved coverage features predate corrected film observations; "
                        "run detailed processing again before using this play for learning."
                    ),
                    clip_id=str(clip_id),
                )
                continue
            if not _valid_graph(packet):
                result.issue(
                    "invalid_graph",
                    (
                        "Coverage graphs need real calibrated inputs, five observed defenders, "
                        "seven finite node features, and valid four-feature edges."
                    ),
                    clip_id=str(clip_id),
                )
                continue
            label, category = truth[clip_id]
            feature = {
                **packet,
                "clip_id": str(clip_id),
                "recording_id": str(job.video_id),
                "session_id": packet_session_id,
                "split": split,
                "canonical_play_id": group,
                "source_job_id": str(job.id),
            }
            example = {
                "clip_id": str(clip_id),
                "canonical_play_id": group,
                "session_id": packet_session_id,
                "split": split,
                "truth": category,
                "label_id": str(label.id),
                "annotated_by": str(label.annotated_by),
                "label_type": "coverage_shell",
                "feature": feature,
            }
            previous = chosen.get(group)
            if previous is None or example["clip_id"] < previous["clip_id"]:
                chosen[group] = example
    result.examples = sorted(
        chosen.values(), key=lambda row: (row["split"], row["canonical_play_id"])
    )
    return result


async def preview_manifest(
    db: AsyncSession, sessions: list[dict[str, Any]], name: str = ""
) -> dict[str, Any]:
    preview = (await _collect(db, sessions)).preview()
    return {**preview, "name": name.strip() or "Coverage learning manifest"}


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return (
        "\n".join(json.dumps(row, sort_keys=True, allow_nan=False) for row in rows) + "\n"
    ).encode("utf-8")


def _write_immutable(key: str, body: bytes) -> str:
    path = local_object_path(ARTIFACTS, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".manifest-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return f"local://{ARTIFACTS}/{key}"


async def create_manifest(
    db: AsyncSession, sessions: list[dict[str, Any]], name: str, actor: User
) -> dict[str, Any]:
    collection = await _collect(db, sessions, lock=True)
    preview = collection.preview()
    if not preview["ready"]:
        raise HTTPException(422, detail=preview)
    manifest_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    name = name.strip() or "Coverage learning manifest"
    feature_rows = [row["feature"] for row in collection.examples]
    label_rows = [
        {
            "schema_version": 1,
            "label_id": row["label_id"],
            "clip_id": row["clip_id"],
            "canonical_play_id": row["canonical_play_id"],
            "session_id": row["session_id"],
            "split": row["split"],
            "label_type": row["label_type"],
            "label_value": {"coverage": row["truth"]},
            "source": "human",
            "annotated_by": row["annotated_by"],
        }
        for row in collection.examples
    ]
    training_rows = [row for row in label_rows if row["split"] == "train"]
    bodies = {
        "features": _jsonl(feature_rows),
        "labels": _jsonl(label_rows),
        "training_labels": _jsonl(training_rows),
    }
    uris = {}
    created = []
    try:
        for kind, body in bodies.items():
            uri = await to_thread.run_sync(
                _write_immutable, f"learning/manifests/{manifest_id}/{kind}.jsonl", body
            )
            uris[kind] = uri
            created.append(uri)
        holdout_ids = [
            uuid.UUID(row["label_id"]) for row in label_rows if row["split"] == "holdout"
        ]
        benchmark = await create_benchmark(
            db, label_ids=holdout_ids, actor=actor, name=f"{name} holdout"[:120]
        )
        for example in collection.examples:
            if example["split"] in {"calibration", "holdout"}:
                for clip in collection.group_clips[example["canonical_play_id"]]:
                    data = clip.label_data if isinstance(clip.label_data, dict) else {}
                    clip.label_data = {
                        **data,
                        "_learning_split": example["split"],
                        "_learning_manifest_id": str(manifest_id),
                    }
        hashes = {
            f"{kind}_sha256": hashlib.sha256(body).hexdigest() for kind, body in bodies.items()
        }
        dataset = TrainingDataset(
            id=dataset_id,
            model_scope="coverage",
            source_label_ids=[row["label_id"] for row in training_rows],
            source_correction_ids=[],
            row_count=len(training_rows),
            artifact_uri=uris["training_labels"],
            created_by=actor.id,
            changelog={
                "schema_version": 1,
                "source": "human",
                "split": "train",
                "manifest_id": str(manifest_id),
                "combined_labels_uri": uris["labels"],
                "features_uri": uris["features"],
                "split_counts": preview["counts"],
                "hashes": hashes,
                "source_feature_hashes": collection.artifact_hashes,
                "calibration_label_ids": [
                    row["label_id"] for row in label_rows if row["split"] == "calibration"
                ],
                "holdout_label_ids": [
                    row["label_id"] for row in label_rows if row["split"] == "holdout"
                ],
                "benchmark_id": benchmark["id"],
            },
        )
        db.add(dataset)
        response = {
            **preview,
            "id": str(manifest_id),
            "name": name,
            "training_dataset_id": str(dataset_id),
            "features_uri": uris["features"],
            "labels_uri": uris["labels"],
            "training_labels_uri": uris["training_labels"],
            "benchmark_id": benchmark["id"],
            "hashes": hashes,
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": str(actor.id),
        }
        db.add(
            SystemSetting(
                key=f"{MANIFEST_PREFIX}{manifest_id}", value=response, updated_by=actor.id
            )
        )
        await db.flush()
        return response
    except BaseException:
        for uri in created:
            _, bucket, key = parse_storage_uri(uri)
            local_object_path(bucket, key).unlink(missing_ok=True)
        raise
