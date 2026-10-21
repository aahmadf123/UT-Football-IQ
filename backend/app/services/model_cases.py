"""Read-only frozen heldout cases, bounded pagination and current film evidence."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobStatus, JobType, ProcessingJob, SystemSetting
from app.services.expected_outcomes import _metrics, _same
from app.services.model_diagnostics import case_document

PREFIX = "learning:cases:"
DIMENSIONS = {
    "capture_regime",
    "source_group",
    "film_category",
    "observed_outcome",
    "error_bin",
    "outcome_pair",
}


def store_cases(
    db: AsyncSession,
    evaluation_id: str,
    model_id: str,
    artifact_sha256: str,
    document: dict[str, Any],
    actor_id: uuid.UUID,
) -> None:
    db.add(
        SystemSetting(
            key=f"{PREFIX}{evaluation_id}",
            updated_by=actor_id,
            value={
                "evaluation_id": evaluation_id,
                "model_id": model_id,
                "artifact_sha256": artifact_sha256,
                "document": document,
            },
        )
    )


async def current_case_films(db: AsyncSession, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """An exact current trusted feature export is required; never find nearest film."""
    from app.services.expected_feature_exports import feature_export_download

    videos = set()
    for case in cases:
        try:
            videos.add(uuid.UUID(case["lineage"]["recording_id"]))
        except (ValueError, KeyError, TypeError):
            continue
    if not videos:
        return {}
    jobs = list(
        (
            await db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.video_id.in_(videos),
                    ProcessingJob.job_type == JobType.pipeline,
                    ProcessingJob.status == JobStatus.succeeded,
                    ProcessingJob.output_artifacts.op("?")("expected_feature_export"),
                )
                .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    exports, seen = {}, set()
    remaining = 64 * 1024 * 1024
    for job in jobs:
        if job.video_id in seen:
            continue
        seen.add(job.video_id)
        descriptor = (job.output_artifacts or {}).get("expected_feature_export") or {}
        size = descriptor.get("size_bytes")
        if type(size) is not int or not 0 < size <= remaining:
            continue
        remaining -= size
        try:
            exported = await feature_export_download(db, job.id, include_content=True)
            exports[str(job.video_id)] = exported, json.loads(exported["content"])
        except HTTPException:
            continue  # Historical cases remain readable, with film explicitly unavailable.
    result = {}
    for case in cases:
        found = exports.get(case["lineage"]["recording_id"])
        if not found:
            continue
        descriptor, document = found
        for identity, packet in document["feature_snapshots"].items():
            if (
                packet.get("task") == case["task"]
                and _same(packet.get("lineage"), case["lineage"])
                and _same(packet.get("observation_clock"), case["observation_clock"])
                and _same(packet.get("subject"), case["subject"])
                and sorted({v["evidence_sha256"] for v in packet["features"].values()})
                == case["feature_evidence_sha256"]
            ):
                clip_id = identity.split(":", 1)[0]
                from app.models import Clip

                clip = await db.get(Clip, uuid.UUID(clip_id))
                if clip is None:
                    continue
                result[case["case_id"]] = {
                    "clip_id": clip_id,
                    "recording_id": document["video_id"],
                    "focus_seconds": case["observation_clock"]["timestamp_seconds"]
                    - clip.start_time,
                    "source_export_job_id": document["job_id"],
                    "source_export_sha256": descriptor["sha256"],
                    **document["clip_revisions"][clip_id],
                }
    return result


async def evaluation_cases(
    db: AsyncSession,
    evaluation_id: uuid.UUID,
    *,
    dimension: str | None = None,
    category: str | None = None,
    offset: int = 0,
    limit: int = 25,
    download: bool = False,
) -> dict[str, Any]:
    if (dimension is None) != (category is None) or (
        dimension is not None and dimension not in DIMENSIONS
    ):
        raise HTTPException(422, "Choose a recorded diagnostic category and dimension")
    evaluation = await db.get(SystemSetting, f"learning:evaluation:{evaluation_id}")
    row = await db.get(SystemSetting, f"{PREFIX}{evaluation_id}")
    if evaluation is None:
        raise HTTPException(404, "Evaluation not found")
    if row is None:
        raise HTTPException(
            404, "This historical evaluation has no frozen heldout case diagnostics"
        )
    try:
        report, stored = evaluation.value, row.value
        document = stored["document"]
        cases = document["cases"]
        if (
            type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or document.get("split") != "holdout"
            or not isinstance(cases, list)
            or not cases
            or stored["evaluation_id"] != str(evaluation_id)
            or stored["model_id"] != report["model_id"]
            or stored["artifact_sha256"] != report["artifact_sha256"]
            or not _same(document, case_document(cases))
            or not _same(
                report["heldout_diagnostics"], {k: v for k, v in document.items() if k != "cases"}
            )
            or document["case_count"] != report["sample_count"]
            or len({c["case_id"] for c in cases}) != len(cases)
            or len({(c["task"], c["lineage"]["canonical_play_id"]) for c in cases}) != len(cases)
        ):
            raise ValueError("Frozen case/report identity or checksum differs")
        selected = [c for c in cases if dimension is None or c[dimension] == category]
        binary = report["task"] in {"completion", "pressure"}
        truth = [c["observed"] for c in selected]
        prediction = [c["predicted"] for c in selected]
        reference = [c["baseline"] for c in selected]
        metrics = _metrics(truth, prediction, binary) if selected else {}
        baseline = _metrics(truth, reference, binary) if selected else {}
        page = selected if download else selected[offset : offset + limit]
        films = {} if download else await current_case_films(db, page)
        return {
            "evaluation_id": str(evaluation_id),
            "model_id": stored["model_id"],
            "artifact_sha256": stored["artifact_sha256"],
            "report_sha256": report.get("report_sha256"),
            "case_sha256": document["sha256"],
            "schema_version": 1,
            "split": "holdout",
            "case_count": len(cases),
            "selected_count": len(selected),
            "offset": 0 if download else offset,
            "groups": document["groups"],
            "filter": {"dimension": dimension, "category": category},
            "known_source_group_count": len({c["source_group"] for c in selected}),
            "canonical_play_count": len({c["lineage"]["canonical_play_id"] for c in selected}),
            "minimum_sample_count": 20,
            "small_sample": len(selected) < 20,
            "descriptive_only": True,
            "release_qualification_inherited": False,
            "metrics": metrics,
            "baseline_metrics": baseline,
            "binary": binary,
            "baseline_definition": report["baseline"]["definition"],
            "cases": [
                {
                    **c,
                    "film": films.get(c["case_id"]),
                    "film_unavailable_reason": None
                    if c["case_id"] in films
                    else (
                        "Source links resolve live in staff review; "
                        "this export does not assert current review state."
                        if download
                        else (
                            "No exact current trusted export matches the source, clock "
                            "and review revision."
                        )
                    ),
                }
                for c in page
            ],
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(422, f"Heldout case diagnostics refused: {exc}") from exc
