"""Qualification for coach label reads; original candidates remain in history."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, JobType, Label, ModelStage, ModelVersion, ProcessingJob, SystemSetting
from app.services.metric_policy import INVALID_SOURCES

LabelQualification = Literal["human_reviewed", "model_approved"]


async def coach_label_qualification(db: AsyncSession, label: Label) -> LabelQualification | None:
    """Caller flags cannot approve a classifier or replace its private receipt."""
    value = label.label_value if isinstance(label.label_value, dict) else {}
    raw_provenance = value.get("provenance")
    provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
    if (
        label.source in INVALID_SOURCES
        or any(
            isinstance(source, str) and source in INVALID_SOURCES
            for source in (value.get("source"), provenance.get("source"))
        )
        or provenance.get("state") == "superseded"
        or provenance.get("publication_state") == "pending"
    ):
        return None
    if label.source in {"human", "manual"}:
        return "human_reviewed" if isinstance(label.annotated_by, uuid.UUID) else None
    if label.source != "model" or not isinstance(label.model_version_id, uuid.UUID):
        return None
    if any(
        layer.get("validated") is False or layer.get("experimental") is True
        for layer in (value, provenance)
    ):
        return None
    model = await db.get(ModelVersion, label.model_version_id)
    if model is None or model.promoted_stage != ModelStage.production:
        return None
    approval = model.metrics if isinstance(model.metrics, dict) else {}
    if (
        not approval.get("approved_evaluation_id")
        or not approval.get("approved_artifact_sha256")
        or provenance.get("artifact_sha256") != approval["approved_artifact_sha256"]
    ):
        return None
    from app.services.learning import canonical_label_type

    expected_type = {
        "coverage_shell": "coverage",
        "offensive_formation": "formation",
        "route_family": "route",
        "personnel_grouping": "personnel",
    }.get(canonical_label_type(label.label_type))
    if expected_type is None or expected_type != model.model_type:
        return None
    from app.services.label_publication import label_scope_approved, require_label_proof

    receipt = await db.get(SystemSetting, f"labels:producer:{label.id}")
    try:
        proof = receipt.value if receipt and isinstance(receipt.value, dict) else {}
        job = await db.get(ProcessingJob, uuid.UUID(proof["job_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    clip = await db.get(Clip, label.clip_id) if label.clip_id else None
    if (
        job is None
        or job.job_type != JobType.pipeline
        or clip is None
        or (clip.provenance or {}).get("published_result_version") != str(job.id)
    ):
        return None
    holds = (job.output_artifacts or {}).get("publication_holds", {})
    if not isinstance(holds, dict) or str(clip.id) in holds:
        return None
    try:
        await require_label_proof(db, label, model)
        if not label_scope_approved(model, label, clip, proof):
            return None
    except HTTPException:
        return None
    return "model_approved"


@dataclass
class CoachLabelSelection:
    labels: list[tuple[Label, LabelQualification]]
    withheld_count: int
    conflicting_types: list[str]


async def select_coach_labels(db: AsyncSession, labels: list[Label]) -> CoachLabelSelection:
    """One effective reviewed meaning per actor/type; conflicts stay unknown."""
    from app.services.learning import canonical_label_type, normalized_label

    grouped: dict[tuple[uuid.UUID | None, str], list[Label]] = defaultdict(list)
    for label in labels:
        grouped[label.tracklet_id, canonical_label_type(label.label_type)].append(label)
    selected = CoachLabelSelection([], 0, [])
    for (_, kind), group in grouped.items():
        human = [
            label
            for label in group
            if label.source in {"human", "manual"}
            and await coach_label_qualification(db, label) == "human_reviewed"
        ]
        if human:
            selected.withheld_count += len(group) - len(human)
            meanings = {normalized_label(label.label_value, kind) for label in human}
            if len(meanings) != 1:
                selected.conflicting_types.append(kind)
                continue
            selected.labels.append((min(human, key=lambda row: str(row.id)), "human_reviewed"))
            continue
        qualified = []
        for label in group:
            qualification = await coach_label_qualification(db, label)
            if qualification is None:
                selected.withheld_count += 1
            else:
                qualified.append((label, qualification))
        # Current model proof can still contain several conflicting outputs;
        # never choose the newest merely to make a coach label available.
        if len({normalized_label(label.label_value, kind) for label, _ in qualified}) > 1:
            selected.withheld_count += len(qualified)
        elif qualified:
            selected.labels.append(min(qualified, key=lambda pair: str(pair[0].id)))
    return selected
