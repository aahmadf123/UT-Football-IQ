"""Source-clock evidence shared by observed position producers.

These helpers do not approve a model. They accept explicit reviewed observations
or artifact-bound approved observations, and never infer bodies from image boxes.
"""

from __future__ import annotations

import math
from itertools import groupby
from typing import Any

from pipeline.homography.field_template import FIELD_LENGTH, HALF_WIDTH
from pipeline.video_timeline import VideoTimeline

VERSION = "2026-10-21.1"
MAX_GAP = 0.2
HIDDEN = {"lost", "offscreen", "occluded", "unobserved"}


def canonical_shot(value: Any) -> str | None:
    """A source shot ID cannot be a placeholder for unresolved continuity."""
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


def known_shot(value: Any) -> bool:
    return canonical_shot(value) is not None


def event_shot(event: dict[str, Any]) -> str | None:
    for value in (
        event.get("shot_id"),
        (event.get("attributes") or {}).get("shot_id"),
        (event.get("provenance") or {}).get("shot_id"),
    ):
        if value is not None:
            return canonical_shot(value)
    return None


def number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def qualified(event: dict[str, Any]) -> bool:
    attrs, prov = event.get("attributes") or {}, event.get("provenance") or {}
    if prov.get("state") in {"superseded", "pending", "suppressed"} or prov.get(
        "publication_state"
    ) in {"superseded", "pending", "suppressed"}:
        return False
    source = event.get("source") or prov.get("source") or attrs.get("source")
    if source in {"human", "manual"}:
        return True
    return (
        source in {"real", "model"}
        and prov.get("validated") is True
        and bool(prov.get("approved_evaluation_id"))
        and bool(prov.get("artifact_sha256"))
        and prov.get("quality_checks_passed") is True
    )


def event_time(event: dict[str, Any], timeline: VideoTimeline | None = None) -> float | None:
    value = number(event.get("timestamp_seconds"))
    if value is None and timeline is not None and event.get("frame_number") is not None:
        value = number(timeline.timestamp(event["frame_number"]))
    return value if value is not None and value >= 0 else None


def evidence(
    events: list[dict[str, Any]], timeline: VideoTimeline | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "event_id": e.get("id"),
            "event_type": e.get("event_type"),
            "timestamp_seconds": event_time(e, timeline),
            "source": e.get("source")
            or (e.get("provenance") or {}).get("source")
            or (e.get("attributes") or {}).get("source"),
            "shot_id": event_shot(e),
        }
        for e in events
    ]


def visible(point: dict[str, Any], *, field: bool = False) -> bool:
    if point.get("visibility_state") in HIDDEN:
        return False
    xy = number(point.get("field_x")), number(point.get("field_y"))
    if all(v is not None for v in xy):
        return -10 <= xy[0] <= FIELD_LENGTH + 10 and -HALF_WIDTH <= xy[1] <= HALF_WIDTH
    return not field and bool(point.get("bbox"))


def samples(track: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = sorted(
        [
            {**p, "shot_id": canonical_shot(p.get("shot_id"))}
            for p in track.get("track_points", [])
            if number(p.get("timestamp_seconds")) is not None
        ],
        key=lambda p: p["timestamp_seconds"],
    )
    result = []
    for _timestamp, values in groupby(ordered, key=lambda p: p["timestamp_seconds"]):
        group = list(values)
        first = group[0]
        conflict = any(
            p.get("visibility_state") in HIDDEN
            or p.get("shot_id") != first.get("shot_id")
            or (p.get("field_x"), p.get("field_y")) != (first.get("field_x"), first.get("field_y"))
            for p in group
        )
        result.append(
            {
                **first,
                "visibility_state": "lost",
                "field_x": None,
                "field_y": None,
                "bbox": None,
                "duplicate_conflict": True,
            }
            if len(group) > 1 and conflict
            else first
        )
    return result


def connected(first: dict[str, Any], last: dict[str, Any], *, field: bool = True) -> bool:
    dt = last["timestamp_seconds"] - first["timestamp_seconds"]
    return (
        0 < dt <= MAX_GAP + 1e-8
        and visible(first, field=field)
        and visible(last, field=field)
        and known_shot(first.get("shot_id"))
        and canonical_shot(first.get("shot_id")) == canonical_shot(last.get("shot_id"))
    )


def sample_at(
    track: dict[str, Any], timestamp: float, *, field: bool = True
) -> dict[str, Any] | None:
    """No extrapolation, cut bridging, or interpolation across hidden samples."""
    pts = samples(track)
    for p in pts:
        if abs(p["timestamp_seconds"] - timestamp) < 1e-8:
            return p if visible(p, field=field) else None
    for a, b in zip(pts, pts[1:]):
        if a["timestamp_seconds"] < timestamp < b["timestamp_seconds"] and connected(
            a, b, field=field
        ):
            ratio = (timestamp - a["timestamp_seconds"]) / (
                b["timestamp_seconds"] - a["timestamp_seconds"]
            )
            result = {**a, "timestamp_seconds": timestamp, "interpolated": True}
            if field:
                result.update(
                    field_x=a["field_x"] + ratio * (b["field_x"] - a["field_x"]),
                    field_y=a["field_y"] + ratio * (b["field_y"] - a["field_y"]),
                )
            return result
    return None


def continuous_prefix(
    track: dict[str, Any], start: float, *, field: bool = False
) -> list[dict[str, Any]]:
    if any(number(p.get("timestamp_seconds")) is None for p in track.get("track_points", [])):
        return []
    first = sample_at(track, start, field=field)
    if first is None:
        return []
    result = [first]
    for point in samples(track):
        if point["timestamp_seconds"] <= start:
            continue
        if not connected(result[-1], point, field=field):
            break
        result.append(point)
    return result


def trajectory(track: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    keys = (
        "timestamp_seconds",
        "frame_number",
        "field_x",
        "field_y",
        "shot_id",
        "visibility_state",
    )
    return [
        {key: p.get(key) for key in keys}
        for p in samples(track)
        if start <= p["timestamp_seconds"] <= end
    ]


def row(
    name: str, packet: dict[str, Any], track_id: str | None = None, unit: str = "s"
) -> dict[str, Any]:
    return {
        "metric_name": name,
        "tracklet_id": track_id,
        "unit": unit,
        "is_suppressed": False,
        "experimental_flag": True,
        "metric_value": {"definition_version": VERSION, **packet},
    }


def original_front(
    tracklets: list[dict[str, Any]],
    snap: dict[str, Any],
    timeline: VideoTimeline | None = None,
    attacking_direction: int | None = None,
) -> dict[str, Any] | None:
    """Resolve original named roles, never the five players nearest a median."""
    if not qualified(snap):
        return None
    start = event_time(snap, timeline)
    attrs = snap.get("attributes") or {}
    los = number(attrs.get("los_x"))
    direction = attrs.get("attacking_direction") or attacking_direction
    if attacking_direction in (-1, 1) and direction != attacking_direction:
        return None
    if (
        start is None
        or los is None
        or not 0 <= los <= FIELD_LENGTH
        or direction not in (-1, 1)
        or isinstance(direction, bool)
    ):
        return None
    by_id = {str(t.get("id")): t for t in tracklets}
    supplied_roles = attrs.get("ol_roles") or {}
    if not isinstance(supplied_roles, dict):
        return None
    roles = {str(role): str(tid) for role, tid in supplied_roles.items()}
    for track in tracklets:
        role = track.get("observed_role")
        if track.get("role_resolved") is True and role in {
            "LT",
            "LG",
            "C",
            "RG",
            "RT",
            "LTE",
            "RTE",
        }:
            tid = str(track["id"])
            if role in roles and roles[role] != tid:
                return None
            roles[role] = tid
    if any(role not in roles for role in ("LT", "LG", "C", "RG", "RT")) or len(
        set(roles.values())
    ) != len(roles):
        return None
    positions = {role: sample_at(by_id.get(tid, {}), start) for role, tid in roles.items()}
    if any(p is None for p in positions.values()):
        return None
    # Positive field y is offensive left when attacking +x. A reversed field
    # attack reverses the lateral ordering, while role names stay original.
    order = ["LT", "LG", "C", "RG", "RT"]
    if any(
        (positions[a]["field_y"] - positions[b]["field_y"]) * direction <= 0
        for a, b in zip(order, order[1:])
    ):
        return None
    return {
        "roles": roles,
        "positions": positions,
        "los_x": los,
        "attacking_direction": direction,
        "start": start,
        "snap": snap,
    }


def named_gaps(front: dict[str, Any]) -> list[dict[str, Any]]:
    pairs = [
        ("left_A", "LG", "C"),
        ("left_B", "LT", "LG"),
        ("right_A", "C", "RG"),
        ("right_B", "RG", "RT"),
    ]
    for side, tackle, te in (("left", "LT", "LTE"), ("right", "RT", "RTE")):
        if te in front["roles"]:
            outward = 1 if side == "left" else -1
            if (front["positions"][te]["field_y"] - front["positions"][tackle]["field_y"]) * front[
                "attacking_direction"
            ] * outward > 0:
                pairs.append((f"{side}_C", te, tackle))
    return [
        {
            "gap_label": label,
            "subject_id": label,
            "initial_pair": [front["roles"][a], front["roles"][b]],
            "y_min": min(front["positions"][a]["field_y"], front["positions"][b]["field_y"]),
            "y_max": max(front["positions"][a]["field_y"], front["positions"][b]["field_y"]),
        }
        for label, a, b in pairs
    ]


def body_bounds(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    result = {key: number(raw.get(key)) for key in ("min_x", "max_x", "min_y", "max_y")}
    if any(v is None for v in result.values()):
        return None
    return (
        result
        if (
            -10 <= result["min_x"] < result["max_x"] <= FIELD_LENGTH + 10
            and -HALF_WIDTH <= result["min_y"] < result["max_y"] <= HALF_WIDTH
        )
        else None
    )


def geometry_snapshot(
    event: dict[str, Any],
    front: dict[str, Any],
    tracklets: list[dict[str, Any]],
    timeline: VideoTimeline | None = None,
) -> dict[str, Any] | None:
    if not qualified(event):
        return None
    attrs = event.get("attributes") or {}
    when = event_time(event, timeline)
    raw_bodies = attrs.get("body_extents") or {}
    if not isinstance(raw_bodies, dict):
        return None
    bodies = {str(tid): body_bounds(raw) for tid, raw in raw_bodies.items()}
    occupancy = attrs.get("defender_occupancy") or {}
    if not isinstance(occupancy, dict):
        return None
    ids = occupancy.get("track_ids")
    if when is None or occupancy.get("complete") is not True or not isinstance(ids, list):
        return None
    # Explicit completeness must cover every observed defender at the sample;
    # unknown offscreen bodies cannot be replaced by a nominal shoulder width.
    known_defenders = {
        str(t["id"])
        for t in tracklets
        if t.get("team_label") == "defense" and sample_at(t, when) is not None
    }
    required = set(front["roles"].values()) | set(map(str, ids)) | known_defenders
    if not known_defenders.issubset(set(map(str, ids))) or any(
        not bodies.get(tid) for tid in required
    ):
        return None
    if any(bounds is None for bounds in bodies.values()):
        return None
    return {"timestamp_seconds": when, "bodies": bodies, "occupancy": occupancy, "event": event}
