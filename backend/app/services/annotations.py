"""Apply coach annotations atomically with their immutable correction history.

The request transaction owns the commit. Clip locks serialize edits; operation IDs
identify retries across restarts, while revisions detect independently edited copies.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Clip,
    CoachCorrection,
    CorrectionType,
    Event,
    Label,
    Metric,
    Player,
    Tracklet,
    User,
    Video,
)
from app.services.clip_state import lock_boundary_scope, require_active_clip

TAG_TYPES = {
    CorrectionType.formation_tag: "offensive_formation",
    CorrectionType.route_tag: "route_family",
    CorrectionType.coverage_tag: "coverage_shell",
    CorrectionType.personnel_tag: "personnel_grouping",
    CorrectionType.leverage_tag: "leverage",
    CorrectionType.effort_tag: "effort",
    CorrectionType.pose_biomechanics_tag: "pose_biomechanics",
}


async def lock_output_id(db: AsyncSession, output_id: uuid.UUID) -> None:
    """Serialize idempotent publication before checking/inserting a stable ID."""
    key = int.from_bytes(output_id.bytes[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def annotation_revision(clip: Clip) -> int:
    data = clip.label_data if isinstance(clip.label_data, dict) else {}
    value = data.get("_annotation_revision", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def check_revision(clip: Clip, expected: int | None) -> None:
    if expected is not None and annotation_revision(clip) != expected:
        raise HTTPException(
            409, detail={"error_code": "annotation_conflict", "revision": annotation_revision(clip)}
        )


def touch_annotation(clip: Clip) -> int:
    revision = annotation_revision(clip) + 1
    clip.label_data = {
        **(clip.label_data if isinstance(clip.label_data, dict) else {}),
        "_annotation_revision": revision,
    }
    return revision


def touch_observations(clip: Clip) -> int:
    """Invalidate saved graphs when observed timing or identities change."""
    data = clip.label_data if isinstance(clip.label_data, dict) else {}
    previous = data.get("_observation_revision", 0)
    revision = previous + 1 if isinstance(previous, int) and not isinstance(previous, bool) else 1
    clip.label_data = {**data, "_observation_revision": revision}
    return revision


def _number(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise HTTPException(422, detail=f"{field} must be a finite nonnegative number")
    return float(value)


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, detail=f"{field} must be a UUID") from exc


async def invalidate_metrics(db: AsyncSession, clip: Clip, reason: str) -> None:
    """Measurements based on edited observations need recomputation, not relabeling."""
    await db.execute(
        update(Metric)
        .where(Metric.clip_id == clip.id)
        .values(
            is_suppressed=True,
            analytics_safe=False,
            suppression_reason=reason,
        )
    )


async def apply_label(
    db: AsyncSession,
    clip: Clip,
    *,
    label_type: str,
    value: dict[str, Any],
    actor_id: uuid.UUID,
    tracklet_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Replace effective labels, preserving previous values in correction history."""
    result = await db.execute(
        select(Label)
        .where(
            Label.clip_id == clip.id,
            Label.label_type == label_type,
            Label.tracklet_id == tracklet_id,
        )
        .with_for_update()
    )
    rows = result.scalars().all()
    originals = [
        {"id": str(row.id), "value": row.label_value, "source": row.source} for row in rows
    ]
    if not rows:
        db.add(
            Label(
                id=uuid.uuid4(),
                clip_id=clip.id,
                tracklet_id=tracklet_id,
                label_type=label_type,
                label_value=dict(value),
                source="human",
                annotated_by=actor_id,
            )
        )
    else:
        for row in rows:
            row.label_value = dict(value)
            row.source = "human"
            row.annotated_by = actor_id
            row.dataset_version = None
            row.training_dataset_id = None
    data = dict(clip.label_data) if isinstance(clip.label_data, dict) else {}
    data[label_type] = dict(value)
    clip.label_data = data
    if label_type == "personnel_grouping":
        personnel = value.get("personnel_grouping", value.get("personnel", value.get("value")))
        if personnel is not None:
            clip.personnel_grouping = str(personnel)[:20]
    return originals


async def submit_correction(
    db: AsyncSession,
    *,
    actor: User,
    clip_id: uuid.UUID,
    correction_type: CorrectionType,
    corrected_value: dict[str, Any],
    original_value: dict[str, Any] | None = None,
    notes: str | None = None,
    training_eligible: bool = True,
    client_operation_id: uuid.UUID | None = None,
    expected_revision: int | None = None,
) -> tuple[CoachCorrection, bool]:
    """Validate, apply and record a correction; return (record, replayed)."""
    if correction_type == CorrectionType.clip_boundary:
        await lock_boundary_scope(db, clip_id)
    if client_operation_id is not None:
        digest = hashlib.sha256(f"{actor.id}:{client_operation_id}".encode()).digest()
        lock = int.from_bytes(digest[:8], "big", signed=True)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        previous = (
            await db.execute(
                select(CoachCorrection).where(
                    CoachCorrection.corrected_by == actor.id,
                    CoachCorrection.corrected_value["_operation_id"].astext
                    == str(client_operation_id),
                )
            )
        ).scalar_one_or_none()
        if previous is not None:
            old_value = previous.corrected_value.get(
                "_request",
                {k: v for k, v in previous.corrected_value.items() if not k.startswith("_")},
            )
            if (
                previous.clip_id != clip_id
                or previous.correction_type != correction_type
                or old_value != corrected_value
            ):
                raise HTTPException(
                    409, detail="Operation ID was already used for different content"
                )
            return previous, True

    clip = (
        await db.execute(select(Clip).where(Clip.id == clip_id).with_for_update())
    ).scalar_one_or_none()
    if clip is None:
        raise HTTPException(404, detail="Clip not found")
    check_revision(clip, expected_revision)
    require_active_clip(clip)
    value = {k: v for k, v in corrected_value.items() if not k.startswith("_")}
    if not value:
        raise HTTPException(422, detail="A correction must include a corrected value")
    before: dict[str, Any] = {}
    if correction_type == CorrectionType.clip_boundary:
        start = value.get("start_seconds", value.get("start_time", clip.start_time))
        end = value.get("end_seconds", value.get("end_time", clip.end_time))
        start, end = _number(start, "start_seconds"), _number(end, "end_seconds")
        if start >= end:
            raise HTTPException(422, detail="start_seconds must be less than end_seconds")
        duration = (
            await db.execute(select(Video.duration_seconds).where(Video.id == clip.video_id))
        ).scalar_one_or_none()
        if isinstance(duration, (int, float)) and end > duration:
            raise HTTPException(422, detail="Boundary exceeds the recording duration")
        if not any(k in value for k in ("start_seconds", "start_time", "end_seconds", "end_time")):
            raise HTTPException(422, detail="Boundary corrections require a start or end time")
        before = {"start_seconds": clip.start_time, "end_seconds": clip.end_time}
        clip.start_time, clip.end_time = start, end
        clip.boundary_source, clip.boundary_confidence = "manual", 1.0
        # Existing extracted media/measurements use the previous boundaries.
        clip.storage_uri = None
    elif correction_type == CorrectionType.player_identity:
        tracklet_id = _uuid(value.get("tracklet_id"), "tracklet_id")
        tracklet = (
            await db.execute(
                select(Tracklet)
                .where(
                    Tracklet.id == tracklet_id,
                    Tracklet.clip_id == clip.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if tracklet is None:
            raise HTTPException(404, detail="Tracklet not found in this clip")
        if (tracklet.provenance or {}).get("state") == "superseded" or (
            tracklet.provenance or {}
        ).get("publication_state") == "pending":
            raise HTTPException(409, detail="Tracking result changed; reload the active tracks")
        before = {
            "tracklet_id": str(tracklet.id),
            "player_id": str(tracklet.player_id) if tracklet.player_id else None,
            "team": tracklet.team_label,
        }
        if not any(k in value for k in ("player_id", "team", "team_label", "jersey_number")):
            raise HTTPException(422, detail="Identity correction requires player, team or jersey")
        if "player_id" in value:
            player_id = _uuid(value["player_id"], "player_id") if value["player_id"] else None
            if (
                player_id is not None
                and (
                    await db.execute(select(Player).where(Player.id == player_id))
                ).scalar_one_or_none()
                is None
            ):
                raise HTTPException(404, detail="Player not found")
            tracklet.player_id = player_id
        team = value.get("team", value.get("team_label"))
        if team is not None:
            if team not in ("home", "away", "unknown"):
                raise HTTPException(422, detail="team must be home, away or unknown")
            tracklet.team_label = team
        if "jersey_number" in value:
            jersey = value["jersey_number"]
            if not isinstance(jersey, int) or isinstance(jersey, bool) or not 0 <= jersey <= 99:
                raise HTTPException(422, detail="jersey_number must be an integer from 0 to 99")
        await apply_label(
            db,
            clip,
            label_type="player_identity",
            value=value,
            actor_id=actor.id,
            tracklet_id=tracklet_id,
        )
    elif correction_type == CorrectionType.event_tag and value.get("issue") == "bad_boxes":
        data = dict(clip.label_data) if isinstance(clip.label_data, dict) else {}
        before = {"tracking_review_required": data.get("tracking_review_required", False)}
        clip.label_data = {**data, "tracking_review_required": True}
        training_eligible = False  # A review flag is not a ground-truth bounding box.
    elif correction_type == CorrectionType.event_tag:
        event_id = _uuid(value["event_id"], "event_id") if value.get("event_id") else None
        event = (
            (
                await db.execute(
                    select(Event)
                    .where(Event.id == event_id, Event.clip_id == clip.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if event_id
            else None
        )
        if event_id and event is None:
            raise HTTPException(404, detail="Event not found in this clip")
        if event is not None and (event.provenance or {}).get("state") == "superseded":
            raise HTTPException(
                409, detail="Removed observations are preserved in history and cannot be edited"
            )
        event_type = value.get("event_type", event.event_type if event else None)
        if not isinstance(event_type, str) or not event_type.strip() or len(event_type) > 100:
            raise HTTPException(422, detail="event_type is required")
        if event:
            before = {
                "event_id": str(event.id),
                "event_type": event.event_type,
                "frame_number": event.frame_number,
                "timestamp_seconds": event.timestamp_seconds,
                "attributes": event.attributes,
            }
        else:
            event = Event(id=uuid.uuid4(), clip_id=clip.id, created_by=actor.id)
            db.add(event)
        previous_attributes = dict(event.attributes) if isinstance(event.attributes, dict) else {}
        previous_timebase = previous_attributes.get("timebase")
        if previous_timebase not in {"clip", "recording", "unknown"}:
            previous_timebase = (
                "clip"
                if previous_attributes.get("source") in {"human", "manual"}
                else "recording"
                if isinstance(event.provenance, dict)
                else "unknown"
            )
        edited_timebase = value.get("timebase", "clip")
        if edited_timebase not in {"clip", "recording"}:
            raise HTTPException(422, detail="timebase must be clip or recording")
        timebase = (
            edited_timebase
            if "timestamp_seconds" in value or "frame_number" in value
            else previous_timebase
        )
        event.event_type = event_type.strip()
        if "timestamp_seconds" in value:
            event.timestamp_seconds = _number(value["timestamp_seconds"], "timestamp_seconds")
            if timebase == "clip" and event.timestamp_seconds > clip.end_time - clip.start_time:
                raise HTTPException(422, detail="Event timestamp exceeds clip duration")
            if (
                timebase == "recording"
                and not clip.start_time <= event.timestamp_seconds <= clip.end_time
            ):
                raise HTTPException(422, detail="Event source timestamp is outside this clip")
            if "frame_number" not in value:
                event.frame_number = None
        if "frame_number" in value:
            frame = value["frame_number"]
            if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
                raise HTTPException(422, detail="frame_number must be a nonnegative integer")
            event.frame_number = frame
            if "timestamp_seconds" not in value:
                event.timestamp_seconds = None
        event.attributes = {
            **previous_attributes,
            **value.get("attributes", {}),
            "source": "human",
            "timebase": timebase,
        }
        event.created_by = actor.id
        event.provenance = {"source": "manual", "validated": True, "timebase": timebase}
        from app.services.actor_bindings import capture_actor_bindings

        # Ignore all client binding metadata. Only the server reads source
        # tracks and manual labels; later workers receive this immutable proof.
        event.attributes = {
            k: v
            for k, v in event.attributes.items()
            if k not in {"actor_bindings", "actor_binding_lineage", "manual_identity_binding"}
        }
        binding = await capture_actor_bindings(db, clip, event.attributes)
        event.provenance = {**event.provenance, "actor_bindings": binding}
        value["event_id"] = str(event.id)
    else:
        label_type = TAG_TYPES[correction_type]
        target = _uuid(value["tracklet_id"], "tracklet_id") if value.get("tracklet_id") else None
        if (
            target
            and (
                await db.execute(
                    select(Tracklet).where(Tracklet.id == target, Tracklet.clip_id == clip.id)
                )
            ).scalar_one_or_none()
            is None
        ):
            raise HTTPException(404, detail="Tracklet not found in this clip")
        before = {
            "labels": await apply_label(
                db, clip, label_type=label_type, value=value, actor_id=actor.id, tracklet_id=target
            )
        }
    if original_value is not None:
        before["client_original"] = original_value
    if correction_type in {
        CorrectionType.clip_boundary,
        CorrectionType.player_identity,
        CorrectionType.event_tag,
    }:
        touch_observations(clip)
    revision = touch_annotation(clip)
    value["_request"] = corrected_value
    value["_revision"] = revision
    if client_operation_id is not None:
        value["_operation_id"] = str(client_operation_id)
    clip.is_reviewed = False
    clip.reviewed_by = None
    await invalidate_metrics(db, clip, "coach_annotation_changed")
    correction = CoachCorrection(
        id=uuid.uuid4(),
        clip_id=clip.id,
        correction_type=correction_type,
        original_value=before,
        corrected_value=value,
        corrected_by=actor.id,
        notes=notes,
        training_eligible=training_eligible,
    )
    db.add(correction)
    await db.flush()
    return correction, False
