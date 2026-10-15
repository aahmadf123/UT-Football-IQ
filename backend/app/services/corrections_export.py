"""Corrections → Labels/TrainingDataset export as a callable service.

Extracted from the /api/v1/corrections/export endpoint so the nightly
scheduler (app.scheduler) can run the same export without an HTTP call.
The router remains the manual/analyst entry point.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

import structlog
from anyio import to_thread
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, CoachCorrection, Label, TrainingDataset
from app.services.annotations import lock_output_id
from app.storage import ARTIFACTS, put_object

log = structlog.get_logger(__name__)


@dataclass
class ExportResult:
    exported_count: int
    label_ids: list[uuid.UUID] = field(default_factory=list)
    training_dataset_id: uuid.UUID | None = None
    artifact_uri: str | None = None


async def export_corrections(
    db: AsyncSession,
    *,
    clip_id: uuid.UUID | None = None,
    model_scope: str = "general",
) -> ExportResult:
    """Export un-exported, training-eligible corrections as Label rows.

    Creates a TrainingDataset snapshot when anything was exported. The caller
    owns the transaction (flush here, commit outside).
    """
    await lock_output_id(db, uuid.uuid5(uuid.NAMESPACE_URL, f"football-iq:export:{model_scope}"))
    q = (
        select(CoachCorrection)
        .join(Clip, Clip.id == CoachCorrection.clip_id)
        .where(
            CoachCorrection.exported_as_label.is_(False),
            CoachCorrection.training_eligible.is_(True),
            func.coalesce(Clip.label_data["_learning_split"].astext, "train").not_in(
                ["holdout", "calibration"]
            ),
            func.coalesce(
                Clip.provenance["source"].astext,
                Clip.label_data["provenance"]["source"].astext,
                Clip.label_data["source"].astext,
                "unknown",
            ).not_in(["synthetic", "synthetic_test", "stub"]),
        )
        .order_by(CoachCorrection.created_at, CoachCorrection.id)
        .limit(1000)
        .with_for_update(of=CoachCorrection, skip_locked=True)
    )
    if clip_id is not None:
        q = q.where(CoachCorrection.clip_id == clip_id)
    result = await db.execute(q)
    corrections = result.scalars().all()

    # Prefetch each involved clip's model_version_id in one query instead of a
    # SELECT-per-correction (an N+1 that scales with correction volume on the
    # nightly path). Missing clip → None model_version_id, same as before.
    clip_ids = {c.clip_id for c in corrections if c.clip_id is not None}
    model_version_by_clip: dict[uuid.UUID, uuid.UUID | None] = {}
    metadata_by_clip: dict[uuid.UUID, dict[str, object]] = {}
    if clip_ids:
        clip_rows = await db.execute(
            select(Clip.id, Clip.model_version_id, Clip.label_data, Clip.provenance).where(
                Clip.id.in_(clip_ids)
            )
        )
        for row in clip_rows.all():
            clip_key, mv_id = row[:2]
            model_version_by_clip[clip_key] = mv_id
            metadata = dict(row[2]) if len(row) > 2 and isinstance(row[2], dict) else {}
            if len(row) > 3 and isinstance(row[3], dict):
                metadata["provenance"] = row[3]
            metadata_by_clip[clip_key] = metadata

    labels: list[Label] = []
    correction_ids: list[uuid.UUID] = []
    records: list[dict[str, object]] = []
    for correction in corrections:
        metadata = metadata_by_clip.get(correction.clip_id, {})
        provenance = metadata.get("provenance", {})
        source = (
            provenance.get("source") if isinstance(provenance, dict) else metadata.get("source")
        )
        if metadata.get("_learning_split") in {"holdout", "calibration"} or source in {
            "synthetic",
            "synthetic_test",
            "stub",
        }:
            continue
        corrected = {k: v for k, v in correction.corrected_value.items() if not k.startswith("_")}
        label = Label(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"football-iq:correction:{correction.id}"),
            clip_id=correction.clip_id,
            label_type=correction.correction_type.value,
            label_value=corrected,
            annotated_by=correction.corrected_by,
            source="human",
            model_version_id=model_version_by_clip.get(correction.clip_id),
        )
        db.add(label)
        correction.exported_as_label = True
        labels.append(label)
        correction_ids.append(correction.id)
        records.append(
            {
                "schema_version": 1,
                "label_id": str(label.id),
                "correction_id": str(correction.id),
                "clip_id": str(correction.clip_id),
                "label_type": label.label_type,
                "label_value": corrected,
                "source": "human",
                "annotated_by": str(correction.corrected_by),
                "annotation_revision": correction.corrected_value.get("_revision"),
                "model_version_id": str(label.model_version_id) if label.model_version_id else None,
            }
        )

    training_dataset_id: uuid.UUID | None = None
    artifact_uri: str | None = None
    if labels:
        previous_result = await db.execute(
            select(TrainingDataset)
            .where(
                TrainingDataset.model_scope == model_scope,
                TrainingDataset.changelog["filter_clip_id"].astext == str(clip_id)
                if clip_id is not None
                else TrainingDataset.changelog["filter_clip_id"].astext.is_(None),
            )
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
            str(c_id) for c_id in correction_ids if c_id not in previous_corrections
        ]

        prior_records: list[dict[str, object]] = []
        if previous is not None and previous.source_label_ids:
            prior_ids = [uuid.UUID(raw) for raw in previous.source_label_ids]
            prior_links = dict(
                zip(previous.source_label_ids, previous.source_correction_ids, strict=False)
            )
            prior_rows = (
                await db.execute(
                    select(Label, Clip.label_data, Clip.provenance)
                    .join(Clip, Clip.id == Label.clip_id)
                    .where(
                        Label.id.in_(prior_ids),
                        Label.clip_id == clip_id if clip_id is not None else true(),
                    )
                )
            ).all()
            for old_label, old_metadata, old_provenance in prior_rows:
                data = old_metadata if isinstance(old_metadata, dict) else {}
                if data.get("_learning_split") in {"holdout", "calibration"} or (
                    isinstance(old_provenance, dict)
                    and old_provenance.get("source") in {"synthetic", "synthetic_test", "stub"}
                ):
                    continue
                if old_label.source != "human":
                    continue
                prior_records.append(
                    {
                        "schema_version": 1,
                        "label_id": str(old_label.id),
                        "correction_id": prior_links.get(str(old_label.id)),
                        "clip_id": str(old_label.clip_id),
                        "label_type": old_label.label_type,
                        "label_value": old_label.label_value,
                        "source": "human",
                        "annotated_by": str(old_label.annotated_by),
                        "model_version_id": str(old_label.model_version_id)
                        if old_label.model_version_id
                        else None,
                    }
                )
        # Each snapshot is the current cumulative pool, with the latest coach
        # answer per target replacing earlier contradictory examples.
        effective: dict[tuple[str, str, str], dict[str, object]] = {}
        for record in [*prior_records, *records]:
            payload = record["label_value"]
            target = str(payload.get("tracklet_id", "")) if isinstance(payload, dict) else ""
            effective[(str(record["clip_id"]), str(record["label_type"]), target)] = record
        records = list(effective.values())

        dataset_id = uuid.uuid4()
        body = ("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n").encode()
        # Persist before marking the transaction exported. A failed disk write
        # rolls back flags/labels; a retried snapshot does not advertise a file
        # that never existed. Scope is metadata, never part of a filesystem key.
        uri = await to_thread.run_sync(
            put_object, ARTIFACTS, f"datasets/{dataset_id}.jsonl", body, "application/x-ndjson"
        )
        artifact_uri = uri
        dataset = TrainingDataset(
            id=dataset_id,
            model_scope=model_scope,
            source_label_ids=[str(record["label_id"]) for record in records],
            source_correction_ids=[
                str(record["correction_id"]) for record in records if record.get("correction_id")
            ],
            row_count=len(records),
            artifact_uri=uri,
            changelog={
                "schema_version": 1,
                "added_correction_ids": added_corrections,
                "sha256": hashlib.sha256(body).hexdigest(),
                "source": "human",
                "split": "train",
                "filter_clip_id": str(clip_id) if clip_id is not None else None,
            },
        )
        db.add(dataset)
        await db.flush()
        training_dataset_id = dataset.id

        for exported_label in labels:
            exported_label.training_dataset_id = training_dataset_id
            exported_label.dataset_version = str(training_dataset_id)

    await db.flush()
    log.info(
        "corrections_exported",
        count=len(labels),
        training_dataset_id=str(training_dataset_id),
    )
    return ExportResult(
        exported_count=len(labels),
        label_ids=[label.id for label in labels],
        training_dataset_id=training_dataset_id,
        artifact_uri=artifact_uri,
    )
