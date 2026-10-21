"""Ingest qualification binds automatic values to approved model/evidence slices."""

import math
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, Event, ModelStage, ModelVersion, PlayView, Tracklet, Video
from app.services.actor_binding_protocol import canonical_shot
from app.services.metric_definitions import DEFINITIONS


async def automatic_exclusion(
    db: AsyncSession,
    *,
    clip: Clip,
    name: str,
    value: dict[str, Any],
    provenance: dict[str, Any],
    model_id: uuid.UUID | None,
    tracklet_id: uuid.UUID | None = None,
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
            and (event.provenance or {}).get("state") not in ("superseded", "pending", "suppressed")
            and (event.provenance or {}).get("publication_state")
            not in ("pending", "superseded", "suppressed")
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

        def source_time(event: Event) -> float | None:
            if event.timestamp_seconds is None or not math.isfinite(event.timestamp_seconds):
                return None
            attributes, event_provenance = event.attributes or {}, event.provenance or {}
            clocks = [
                value["timebase"] for value in (attributes, event_provenance) if "timebase" in value
            ]
            if (
                any(
                    not isinstance(clock, str) or clock not in {"clip", "recording"}
                    for clock in clocks
                )
                or len(set(clocks)) > 1
            ):
                return None
            if not clocks:
                return None
            timebase = clocks[0]
            value = event.timestamp_seconds + (clip.start_time if timebase == "clip" else 0)
            return value if clip.start_time <= value <= clip.end_time else None

        if len(snaps) != 1 or len(releases) != 1:
            return "required_event_evidence_missing"
        snap, release = snaps[0], releases[0]

        def known_shot(event: Event) -> str | None:
            values = [
                canonical_shot(value["shot_id"])
                for value in (event.attributes or {}, event.provenance or {})
                if "shot_id" in value
            ]
            return values[0] if values and None not in values and len(set(values)) == 1 else None

        snap_shot, release_shot = known_shot(snap), known_shot(release)
        first, last = source_time(snap), source_time(release)

        for key, expected in (("source_start_seconds", first), ("source_end_seconds", last)):
            if key in value and (
                expected is None
                or not isinstance(value[key], (float, int))
                or isinstance(value[key], bool)
                or not math.isfinite(value[key])
                or abs(value[key] - expected) > 0.002
            ):
                return "required_event_evidence_missing"

        if (
            isinstance(seconds, (float, int))
            and not isinstance(seconds, bool)
            and math.isfinite(seconds)
            and seconds > 0
            and snap_shot is not None
            and snap_shot == release_shot
            and first is not None
            and last is not None
        ):
            if abs((last - first) - seconds) <= 0.002 and last > first:
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
    from app.services.expected_outcomes import CLOCKS, FEATURE_VERSION, POPULATIONS, TASK_TYPES

    if name in TASK_TYPES.values():
        task = next(task for task, metric_name in TASK_TYPES.items() if metric_name == name)
        clock = value.get("observation_clock")
        scalar = value.get("value")
        feature_hashes = value.get("feature_evidence_sha256")
        if (
            provenance.get("source") != "real"
            or model.model_type != name
            or provenance.get("validated") is not True
            or value.get("validated") is not True
            or value.get("experimental") is not False
            or value.get("analytics_safe") is not True
            or provenance.get("approved_evaluation_id") != metrics["approved_evaluation_id"]
            or value.get("weights_sha256") != metrics["approved_artifact_sha256"]
            or value.get("task") != task
            or value.get("feature_version") != FEATURE_VERSION
            or approval.get("feature_version") != FEATURE_VERSION
            or value.get("eligible_population") != POPULATIONS[task]
            or not isinstance(clock, dict)
            or clock.get("event") != CLOCKS[task]
            or not isinstance(clock.get("timestamp_seconds"), (int, float))
            or isinstance(clock.get("timestamp_seconds"), bool)
            or not math.isfinite(clock["timestamp_seconds"])
            or clock["timestamp_seconds"] < 0
            or not isinstance(scalar, (int, float))
            or isinstance(scalar, bool)
            or not math.isfinite(scalar)
            or (task in {"completion", "pressure"} and not 0 <= scalar <= 1)
            or not isinstance(feature_hashes, list)
            or not feature_hashes
            or any(
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for digest in feature_hashes
            )
        ):
            return "expected_outcome_artifact_or_snapshot_mismatch"
        video = await db.get(Video, clip.video_id)
        from app.services.media_identity import verified_media_sha256

        media_sha256 = await verified_media_sha256(db, video) if video is not None else None
        canonical = (
            await db.execute(select(PlayView.play_id).where(PlayView.clip_id == clip.id))
        ).scalar_one_or_none() or clip.id
        lineage = value.get("lineage")
        if (
            video is None
            or not video.practice_session_id
            or not media_sha256
            or not isinstance(lineage, dict)
            or any(
                lineage.get(key) != expected
                for key, expected in {
                    "recording_id": str(video.id),
                    "media_sha256": media_sha256,
                    "session_id": str(video.practice_session_id),
                    "canonical_play_id": str(canonical),
                }.items()
            )
        ):
            return "prediction_source_lineage_mismatch"
        if not clip.start_time <= clock["timestamp_seconds"] <= clip.end_time:
            return "prediction_clock_outside_clip"
        subject = value.get("subject")
        if not isinstance(subject, dict):
            return "prediction_subject_unresolved"
        if task != "pressure":
            if subject.get("scope") != "player":
                return "prediction_subject_unresolved"
            if tracklet_id is None or subject.get("tracklet_id") != str(tracklet_id):
                return "prediction_actor_mismatch"
            actor = await db.get(Tracklet, tracklet_id)
            expected_role = {"completion": "QB", "rushing_gain": "rusher", "yac": "receiver"}.get(
                task
            )
            if actor is None or actor.clip_id != clip.id or subject.get("role") != expected_role:
                return "prediction_actor_mismatch"
        elif subject.get("scope") != "team" or tracklet_id is not None or not subject.get("reason"):
            return "prediction_actor_mismatch"
    regime = str(clip.capture_regime) if clip.capture_regime else "unknown"
    if name in TASK_TYPES.values() and value.get("capture_regime") != regime:
        return "prediction_film_category_mismatch"
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
