"""native-observations-v1; mirrored verbatim in backend services.

No rounding or tolerance matching. Calibration and confidence are deliberately
excluded: identity binds the complete image observations, not a field transform.
Numbers are finite IEEE-754 hex strings, so JSON integer/float transport agrees.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PROTOCOL = "native-observations-v1"
ACTOR_FIELDS = frozenset(
    {
        "actor_id",
        "qb_id",
        "carrier_id",
        "ball_carrier_id",
        "runner_id",
        "blocker_id",
        "defender_id",
        "opponent_id",
        "ball_id",
        "center_id",
        "player_track_id",
        "receiver_id",
        "rusher_id",
        "kicker_id",
        "tracklet_id",
        "opponent_tracklet_id",
        "intended_receiver_id",
        "possessor_id",
    }
)
ACTOR_LIST_FIELDS = frozenset(
    {
        "defender_track_ids",
        "called_rusher_ids",
        "called_blitzer_ids",
        "protection_blocker_ids",
        "front_track_ids",
    }
)
ACTOR_MAP_KEY_FIELDS = frozenset({"body_extents", "defensive_roles", "dl_gap_alignment"})


def canonical_shot(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in {
        "unknown",
        "unverified",
        "unresolved",
        "none",
        "null",
        "missing",
        "n/a",
    }:
        return None
    return str(int(value)) if value.isdecimal() else value


def _number(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    return (0.0 if value == 0 else value).hex() if math.isfinite(value) else None


def observation_fingerprint(track: dict[str, Any]) -> str | None:
    points = track.get("track_points")
    if not isinstance(points, list) or not points:
        return None
    rows = []
    frames = set()
    for point in points:
        if not isinstance(point, dict):
            return None
        frame, when = point.get("frame_number"), point.get("timestamp_seconds")
        shot, state = canonical_shot(point.get("shot_id")), point.get("visibility_state")
        if (
            not isinstance(frame, int)
            or isinstance(frame, bool)
            or frame < 0
            or frame in frames
            or not isinstance(when, (int, float))
            or isinstance(when, bool)
            or _number(when) is None
            or when < 0
            or shot is None
            or state not in {"visible", "observed", "occluded", "offscreen", "lost", "predicted"}
        ):
            return None
        bbox = point.get("bbox")
        if bbox is None:
            if state in {"visible", "observed"}:
                return None
        elif (
            not isinstance(bbox, (tuple, list))
            or len(bbox) != 4
            or any(_number(x) is None for x in bbox)
        ):
            return None
        frames.add(frame)
        rows.append(
            [
                frame,
                _number(when),
                shot,
                state,
                [_number(x) for x in bbox] if bbox is not None else None,
            ]
        )
    rows.sort(key=lambda row: row[0])
    if any(
        float.fromhex(b[1]) <= float.fromhex(a[1]) for a, b in zip(rows, rows[1:], strict=False)
    ):
        return None
    data = json.dumps([PROTOCOL, rows], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def actor_references(attrs: dict[str, Any]) -> set[str]:
    refs = {str(attrs[k]) for k in ACTOR_FIELDS if attrs.get(k) is not None}
    roles = attrs.get("ol_roles")
    if isinstance(roles, dict):
        refs.update(str(v) for v in roles.values() if v is not None)
    for key in ACTOR_LIST_FIELDS:
        if isinstance(attrs.get(key), list):
            refs.update(str(v) for v in attrs[key] if v is not None)
    for key in ACTOR_MAP_KEY_FIELDS:
        if isinstance(attrs.get(key), dict):
            refs.update(str(k) for k in attrs[key])
    occupancy = attrs.get("defender_occupancy")
    if isinstance(occupancy, dict) and isinstance(occupancy.get("track_ids"), list):
        refs.update(str(v) for v in occupancy["track_ids"])
    return refs


def reference_shape_errors(attrs: dict[str, Any]) -> list[str]:
    """Recognized actor containers are typed; malformed inputs are not repaired."""
    errors = []
    for key in ACTOR_FIELDS:
        if attrs.get(key) is not None and (not isinstance(attrs[key], str) or not attrs[key]):
            errors.append(key)
    for key in ACTOR_LIST_FIELDS:
        if key in attrs and (
            not isinstance(attrs[key], list)
            or any(not isinstance(v, str) or not v for v in attrs[key])
        ):
            errors.append(key)
    for key in ACTOR_MAP_KEY_FIELDS | {"ol_roles"}:
        if key in attrs and not isinstance(attrs[key], dict):
            errors.append(key)
    if isinstance(attrs.get("ol_roles"), dict) and any(
        v is not None and (not isinstance(v, str) or not v) for v in attrs["ol_roles"].values()
    ):
        errors.append("ol_roles")
    if "defender_occupancy" in attrs:
        occupancy = attrs["defender_occupancy"]
        if not isinstance(occupancy, dict) or (
            "track_ids" in occupancy
            and (
                not isinstance(occupancy["track_ids"], list)
                or any(not isinstance(v, str) or not v for v in occupancy["track_ids"])
            )
        ):
            errors.append("defender_occupancy")
    return sorted(set(errors))


def rewrite_actor_references(attrs: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    from copy import deepcopy

    result = deepcopy(attrs)
    for key in ACTOR_FIELDS:
        if result.get(key) is not None:
            result[key] = mapping[str(result[key])]
    if isinstance(result.get("ol_roles"), dict):
        result["ol_roles"] = {
            k: mapping[str(v)] if v is not None else None for k, v in result["ol_roles"].items()
        }
    for key in ACTOR_LIST_FIELDS:
        if isinstance(result.get(key), list):
            result[key] = [mapping[str(v)] if v is not None else None for v in result[key]]
    for key in ACTOR_MAP_KEY_FIELDS:
        if isinstance(result.get(key), dict):
            result[key] = {mapping[str(k)]: v for k, v in result[key].items()}
    occupancy = result.get("defender_occupancy")
    if isinstance(occupancy, dict) and isinstance(occupancy.get("track_ids"), list):
        occupancy["track_ids"] = [mapping[str(v)] for v in occupancy["track_ids"]]
    return result
