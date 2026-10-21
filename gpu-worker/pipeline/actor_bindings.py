"""Replay server-captured actor references only through unique exact evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pipeline.actor_binding_protocol import (
    PROTOCOL,
    actor_references,
    observation_fingerprint,
    reference_shape_errors,
    rewrite_actor_references,
)

SCOPE_KEYS = ("video_id", "clip_id", "start_time", "end_time", "registration_fingerprint", "media_sha256")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def rebind_reviewed_events(
    events: list[dict[str, Any]], tracks: list[dict[str, Any]], scope: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return copies; rejected events stay in the saved annotation, never stages.

    Full media identity is required. Registration hashes can be sampled and are
    checked independently; they are never compared to a full worker SHA-256.
    """
    current_tracks, accepted, issues = deepcopy(tracks), [], []
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    by_id: dict[str, list[dict[str, Any]]] = {}
    for track in current_tracks:
        by_id.setdefault(str(track.get("id")), []).append(track)
        digest = observation_fingerprint(track)
        if digest and str(track.get("clip_id")) == str(scope.get("clip_id")):
            by_fingerprint.setdefault(digest, []).append(track)
    # One source actor must have one current actor across the entire snapshot.
    claims: dict[str, set[str]] = {}
    for event in events:
        binding = _mapping(_mapping(event.get("provenance")).get("actor_bindings"))
        for old, ref in _mapping(binding.get("references")).items():
            if isinstance(ref, dict):
                claims.setdefault(str(ref.get("observation_sha256")), set()).add(str(old))
    for original in events:
        attrs = _mapping(original.get("attributes"))
        refs = actor_references(attrs)
        shape_errors = reference_shape_errors(attrs)
        if shape_errors:
            issues.append({"event_id": original.get("id"), "review_required": True,
                           "reason": "actor_reference_container_invalid", "fields": shape_errors,
                           "actor_ids": sorted(refs)})
            continue
        if not refs:
            accepted.append(deepcopy(original))
            continue
        binding = _mapping(_mapping(original.get("provenance")).get("actor_bindings"))
        reason, mapping = None, {}
        saved_scope = _mapping(binding.get("scope"))
        if binding.get("protocol") != PROTOCOL:
            reason = "actor_binding_missing_or_protocol_unknown"
        elif any(scope.get(k) is None or saved_scope.get(k) != scope.get(k) for k in SCOPE_KEYS):
            reason = "actor_binding_source_clip_or_window_mismatch"
        elif binding.get("review_required"):
            reason = "actor_binding_capture_incomplete"
        else:
            for old in sorted(refs):
                reference = _mapping(_mapping(binding.get("references")).get(old))
                digest = reference.get("observation_sha256")
                matches = by_fingerprint.get(str(digest), [])
                if reference.get("original_track_id") != old or not digest or not matches:
                    reason = "actor_binding_observations_missing_or_changed"
                    break
                if (len(matches) != 1 or len(claims.get(str(digest), set())) != 1
                        or len(by_id.get(str(matches[0].get("id")), [])) != 1):
                    reason = "actor_binding_observations_ambiguous"
                    break
                mapping[old] = str(matches[0]["id"])
        if reason:
            issues.append({"event_id": original.get("id"), "reason": reason,
                           "review_required": True, "actor_ids": sorted(refs)})
            continue
        rebound = deepcopy(original)
        rebound["attributes"] = rewrite_actor_references(attrs, mapping)
        rebound["provenance"] = {**(rebound.get("provenance") or {}),
                                  "actor_binding_lineage": mapping}
        accepted.append(rebound)
        for old, new in mapping.items():
            identity = _mapping(binding["references"][old].get("manual_identity"))
            if (identity.get("source") not in {"human", "manual"} or not identity.get("label_id")
                    or not identity.get("annotated_by") or not identity.get("player_id")
                    or identity.get("original_track_id") != old):
                continue
            target = by_id[new][0]
            target.update({"player_id": identity["player_id"], "identity_state": "resolved",
                           "identity_verified": True})
            target["provenance"] = {**(target.get("provenance") or {}),
                                    "identity": {"source": "manual", "label_id": identity["label_id"]},
                                    "manual_identity_binding": {**identity, "current_track_id": new}}
    return accepted, current_tracks, issues
