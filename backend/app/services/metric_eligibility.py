"""Ingest qualification binds automatic values to approved model/evidence slices."""

import math
import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, Event, ModelStage, ModelVersion
from app.services.metric_definitions import DEFINITIONS


async def automatic_exclusion(
    db: AsyncSession,
    *,
    clip: Clip,
    name: str,
    value: dict[str, Any],
    provenance: dict[str, Any],
    model_id: uuid.UUID | None,
) -> str | None:
    """A worker's validated boolean alone never approves a new film population."""
    if provenance.get("validation_source") == "human_event_timestamps" and name == "time_to_throw":
        if value.get("right_censored") is True:
            return "forward_pass_release_not_observed"
        events = (await db.execute(select(Event).where(Event.clip_id == clip.id))).scalars().all()
        trusted = [
            event
            for event in events
            if (
                (event.provenance or {}).get("source") in {"manual", "human"}
                or (event.attributes or {}).get("source") in {"manual", "human"}
            )
            and event.timestamp_seconds is not None
            and math.isfinite(event.timestamp_seconds)
            and (event.provenance or {}).get("state") != "superseded"
            and (event.provenance or {}).get("publication_state") != "pending"
        ]
        snaps = [event for event in trusted if event.event_type == "snap"]
        releases = [
            event
            for event in trusted
            # Legacy `throw` is the documented forward-pass event. A generic
            # release needs explicit forward-pass evidence (not a handoff).
            if event.event_type in {"throw", "pass_release", "forward_pass_release"}
            or (
                event.event_type == "release"
                and (
                    (event.attributes or {}).get("forward_pass") is True
                    or (event.attributes or {}).get("release_type") == "forward_pass"
                )
            )
        ]
        seconds = value.get("seconds")

        def source_time(event: Event) -> float:
            attributes, event_provenance = event.attributes or {}, event.provenance or {}
            timebase = attributes.get("timebase") or event_provenance.get("timebase") or "clip"
            return cast(float, event.timestamp_seconds) + (
                clip.start_time if timebase == "clip" else 0
            )

        if (
            isinstance(seconds, (float, int))
            and not isinstance(seconds, bool)
            and math.isfinite(seconds)
            and seconds >= 0
        ):
            if any(
                abs((source_time(release) - source_time(snap)) - seconds) <= 0.002
                and source_time(release) >= source_time(snap)
                for snap in snaps
                for release in releases
            ):
                return None
        return "required_event_evidence_missing"
    model = await db.get(ModelVersion, model_id) if model_id else None
    if model is None or model.promoted_stage != ModelStage.production:
        return "model_not_approved"
    metrics = model.metrics or {}
    if not metrics.get("approved_evaluation_id") or not metrics.get("approved_artifact_sha256"):
        return "model_approval_evidence_missing"
    if provenance.get("artifact_sha256") != metrics["approved_artifact_sha256"]:
        return "model_artifact_mismatch"
    approval = (metrics.get("publication_approvals") or {}).get(name)
    if not isinstance(approval, dict):
        return "measurement_not_approved"
    regime = str(clip.capture_regime) if clip.capture_regime else "unknown"
    if regime not in approval.get("capture_regimes", []):
        return "film_category_not_approved"
    definition = DEFINITIONS.get(name)
    version = provenance.get("definition_version") or value.get("definition_version")
    if (
        not definition
        or version != approval.get("definition_version")
        or version != definition["version"]
    ):
        return "definition_not_approved"
    evidence = provenance.get("evidence")
    if not isinstance(evidence, dict) or any(
        evidence.get(required) is None or evidence.get(required) is False
        for required in definition["required_evidence"]
    ):
        return "required_evidence_missing"
    if provenance.get("quality_checks_passed") is not True:
        return "quality_gate_failed"
    return None
