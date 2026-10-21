"""Conservative replay of server-recorded staff removals, with no nearest match."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.actor_bindings import SCOPE_KEYS, rebind_reviewed_events
from pipeline.position_evidence import canonical_shot, number


def _dict(value):
    return value if isinstance(value, dict) else {}


def _human(event):
    return any(
        layer.get("source") in {"human", "manual"}
        for layer in (
            event,
            _dict(event.get("attributes")),
            _dict(event.get("provenance")),
        )
    )


def _clock(event, scope, timeline):
    if timeline is None or timeline.verified is not True:
        return None
    attrs, provenance = _dict(event.get("attributes")), _dict(event.get("provenance"))
    timebase = attrs.get("timebase") or provenance.get("timebase")
    if timebase not in {"clip", "recording"}:
        return None
    when, frame = number(event.get("timestamp_seconds")), event.get("frame_number")
    if when is not None:
        when += scope["start_time"] if timebase == "clip" else 0
        if when < scope["start_time"] or when > scope["end_time"]:
            return None
        if frame is None or timebase == "clip":
            frame = timeline.frame_for_time(when)
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        return None
    native = timeline.timestamp(frame)
    if native is None or (when is not None and native != when):
        return None
    if not scope["start_time"] <= native <= scope["end_time"]:
        return None
    shots = {
        canonical_shot(value)
        for value in (event.get("shot_id"), attrs.get("shot_id"), provenance.get("shot_id"))
        if value is not None
    }
    return (frame, native, next(iter(shots))) if len(shots) == 1 and None not in shots else None


def _attributes(event, scope):
    attrs = deepcopy(_dict(event.get("attributes")))
    clock = attrs.get("interval_timebase") or attrs.get("timebase")
    if "start_seconds" in attrs or "end_seconds" in attrs:
        if clock not in {"clip", "recording"}:
            return None
        if clock == "clip":
            for key in ("start_seconds", "end_seconds"):
                if key in attrs:
                    value = number(attrs[key])
                    if value is None:
                        return None
                    attrs[key] = value + scope["start_time"]
    metadata = {
        "source",
        "provenance",
        "timebase",
        "interval_timebase",
        "shot_id",
        "timestamp_verified",
        "review_required",
        "confidence",
        "model_confidence",
        "detection_confidence",
        "model_version_id",
        "artifact_sha256",
    }
    return {key: value for key, value in attrs.items() if key not in metadata}


def suppress_removed_proposals(
    proposals: list[dict[str, Any]],
    removals: list[dict[str, Any]],
    tracks: list[dict[str, Any]],
    scope: dict[str, Any],
    timeline,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Only exact model proposals are suppressed; independent humans survive.

    Unknown source proof/native clocks and changed/ambiguous observations abstain.
    Saved tombstones are immutable; failure to match is not an inferred outcome.
    """
    known, issues = [], []
    for tombstone in removals:
        removal = _dict(_dict(tombstone.get("provenance")).get("removal"))
        original, binding = (
            _dict(removal.get("original_event")),
            _dict(removal.get("actor_bindings")),
        )
        saved_scope = _dict(binding.get("scope"))
        if (
            not removal.get("correction_id")
            or removal.get("source") != "human"
            or original.get("id") != tombstone.get("id")
            or any(
                scope.get(key) is None or saved_scope.get(key) != scope.get(key)
                for key in SCOPE_KEYS
            )
            or _clock(original, scope, timeline) is None
        ):
            issues.append(
                {"event_id": tombstone.get("id"), "reason": "removed_event_identity_unresolved"}
            )
            continue
        known.append(
            {
                **deepcopy(original),
                "provenance": {**_dict(original.get("provenance")), "actor_bindings": binding},
            }
        )
    rebound, _, failed = rebind_reviewed_events(known, tracks, scope)
    rebound_by_id = {event.get("id"): event for event in rebound}
    issues.extend(
        {"event_id": issue["event_id"], "reason": "removed_event_identity_unresolved"}
        for issue in failed
    )
    kept = []
    for proposal in proposals:
        if _human(proposal):
            kept.append(proposal)
            continue
        matched = False
        for original in known:
            if proposal.get("event_type") != original.get("event_type") or _clock(
                proposal, scope, timeline
            ) != _clock(original, scope, timeline):
                continue
            rebound_original = rebound_by_id.get(original.get("id"))
            observed = _attributes(rebound_original, scope) if rebound_original else None
            if observed is not None and observed == _attributes(proposal, scope):
                matched = True
                break
        if not matched:
            kept.append(proposal)
    return kept, issues
