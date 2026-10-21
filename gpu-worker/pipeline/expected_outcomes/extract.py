"""Extract prediction-time features from qualified events and exact source-PTS tracks.

No future samples, nearest-frame extrapolation, guessed targets or inferred zeros.
Complete defender/front populations must be explicitly qualified at the clock.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from pipeline.expected_outcomes.data import (
    DataError,
    definitions,
    feature_artifact,
    finite,
    validate_snapshot,
)
from pipeline.position_evidence import (
    connected,
    event_shot,
    event_time,
    known_shot,
    qualified,
    samples,
    visible,
)


def _real_observation(packet: dict[str, Any]) -> None:
    for layer in (packet, packet.get("provenance") or {}, packet.get("attributes") or {}):
        source = str(layer.get("source", "")).lower()
        if any(marker in source for marker in ("synthetic", "stub", "mock", "test", "placeholder")):
            raise DataError("Known synthetic/stub/test evidence cannot become a real feature")


def _point(track: dict[str, Any], when: float) -> dict[str, Any]:
    _real_observation(track)
    if not qualified(track):
        raise DataError("Track observations are not reviewed or artifact-qualified")
    found = [p for p in samples(track) if abs(p["timestamp_seconds"] - when) < 1e-8]
    for point in track.get("track_points", []):
        if point.get("timestamp_seconds") == when:
            _real_observation(point)
    if (
        len(found) != 1
        or not visible(found[0], field=True)
        or not known_shot(found[0].get("shot_id"))
        or found[0].get("visibility_state") not in {"observed", "visible"}
    ):
        raise DataError("No exact qualified field sample at prediction clock")
    return found[0]


def _distance(a: dict, b: dict) -> float:
    return math.hypot(a["field_x"] - b["field_x"], a["field_y"] - b["field_y"])


def _pressure_values(tracks, event, when, shot, attrs, direction, down, distance):
    """Version 2 uses reviewed original roles and actual known pre-snap calls."""
    if (
        attrs.get("front_population_complete") is not True
        or attrs.get("defender_population_complete") is not True
        or type(direction) is not int
        or direction not in (-1, 1)
        or type(down) is not int
        or down not in (1, 2, 3, 4)
        or distance is None
        or finite(distance) < 0
        or type(attrs.get("down")) is not int
        or type(attrs.get("attacking_direction")) is not int
        or attrs.get("down") != down
        or attrs.get("distance_yards") != distance
        or attrs.get("attacking_direction") != direction
    ):
        raise DataError("Pressure front, orientation or down/distance incomplete")
    ol_roles, defensive_roles = attrs.get("ol_roles"), attrs.get("defensive_roles")
    order = ("LT", "LG", "C", "RG", "RT")
    if (
        not isinstance(ol_roles, dict)
        or not set(order).issubset(ol_roles)
        or set(ol_roles) - set(order) - {"LTE", "RTE"}
        or any(not isinstance(identity, str) or not identity for identity in ol_roles.values())
        or len(set(ol_roles.values())) != len(ol_roles)
    ):
        raise DataError("Pressure requires explicit qualified original LT/LG/C/RG/RT roles")
    ids = attrs.get("defender_track_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or len(set(ids)) != len(ids)
        or not isinstance(defensive_roles, dict)
        or set(defensive_roles) != set(ids)
    ):
        raise DataError("Pressure requires the complete explicitly named defensive-role population")
    dl_roles, lb_roles = {"DL", "DE", "DT", "NT"}, {"LB", "ILB", "OLB", "MLB"}
    allowed = dl_roles | lb_roles | {"CB", "DB", "S", "FS", "SS", "NB"}
    if any(role not in allowed for role in defensive_roles.values()) or set(ids).intersection(
        ol_roles.values()
    ):
        raise DataError("Unresolved or contradictory defensive roles")
    if (
        attrs.get("call_context_known") is not True
        or not 0 <= finite(attrs.get("call_context_available_at_seconds")) <= when
    ):
        raise DataError("Pressure requires actual call context available at the snap")
    calls = {}
    for field in ("called_rusher_ids", "called_blitzer_ids", "protection_blocker_ids"):
        supplied = attrs.get(field)
        if (
            not isinstance(supplied, list)
            or len(set(supplied)) != len(supplied)
            or any(not isinstance(tid, str) or not tid for tid in supplied)
        ):
            raise DataError(
                "Explicit qualified rush, blitz and protection call identities required"
            )
        calls[field] = set(supplied)
    if (
        not calls["protection_blocker_ids"]
        or not calls["called_rusher_ids"].issubset(set(ids))
        or calls["protection_blocker_ids"].intersection(ids)
        or not calls["called_blitzer_ids"].issubset(calls["called_rusher_ids"])
    ):
        raise DataError("Called rush/blitz/protection populations are inconsistent")
    points, used = {}, []
    required = set(ol_roles.values()) | set(ids) | calls["protection_blocker_ids"]
    for identity in sorted(required):
        track = tracks.get(identity)
        if track is None:
            raise DataError("Explicit pressure population track is missing")
        offensive = identity in set(ol_roles.values()) | calls["protection_blocker_ids"]
        if track.get("side_of_ball") in {"offense", "defense"} and track["side_of_ball"] != (
            "offense" if offensive else "defense"
        ):
            raise DataError("Pressure role/call contradicts known team side")
        expected = next(
            (role for role, tid in ol_roles.items() if tid == identity),
            defensive_roles.get(identity),
        )
        if (
            expected is not None
            and track.get("role_resolved") is True
            and track.get("observed_role")
            not in {
                expected,
                "TE" if expected in {"LTE", "RTE"} else "OL" if offensive else expected,
            }
        ):
            raise DataError("Pressure context contradicts the explicitly observed role")
        point = _point(track, when)
        if point["shot_id"] != shot or point.get("frame_number") != event.get("frame_number"):
            raise DataError("Pressure role samples disagree with snap shot/source clock")
        points[identity] = point
        used.append({**track, "track_points": [point]})
    ys = [points[ol_roles[role]]["field_y"] for role in order]
    if any((a - b) * direction <= 0 for a, b in zip(ys, ys[1:])):
        raise DataError("Original OL roles conflict with the qualified field orientation")
    gaps = [abs(a - b) for a, b in zip(ys, ys[1:])]
    dl = [tid for tid, role in defensive_roles.items() if role in dl_roles]
    lbs = [tid for tid, role in defensive_roles.items() if role in lb_roles]
    if not dl or not lbs:
        raise DataError("Pressure requires observed DL and LB role populations")
    alignment = attrs.get("dl_gap_alignment")
    if (
        not isinstance(alignment, dict)
        or set(alignment) != set(dl)
        or any(gap not in {"a", "b", "c", "edge"} for gap in alignment.values())
    ):
        raise DataError("Pressure requires actual qualified DL gap-alignment evidence")
    los, ball_y = finite(attrs.get("los_x")), finite(attrs.get("ball_y"))
    if not 0 <= los <= 100 or not -26.6666666667 <= ball_y <= 26.6666666667:
        raise DataError("Pressure LOS or ball position is outside canonical field bounds")
    depths = [direction * (points[tid]["field_x"] - los) for tid in lbs]
    if any(depth < 0 for depth in depths):
        raise DataError("Observed linebacker depth is outside the supported feature population")
    values = {
        "mean_gap_width": sum(gaps) / len(gaps),
        "max_gap_width": max(gaps),
        "dl_count": len(dl),
        "box_count": sum(
            0 <= direction * (points[tid]["field_x"] - los) <= 5
            and abs(points[tid]["field_y"] - ball_y) <= 9
            for tid in ids
        ),
        "rusher_blocker_diff": len(calls["called_rusher_ids"])
        - len(calls["protection_blocker_ids"]),
        "lb_count": len(lbs),
        "mean_lb_depth": sum(depths) / len(depths),
        "min_lb_depth": min(depths),
        "blitzers": len(calls["called_blitzer_ids"]),
        "down": down,
        "distance_yards": distance,
        **{
            f"dl_in_{gap}_gap" if gap != "edge" else "dl_on_edge": sum(
                value == gap for value in alignment.values()
            )
            for gap in ("a", "b", "c", "edge")
        },
    }
    return values, used


def extract_snapshot(
    task: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    lineage: dict[str, str],
    *,
    analytics_safe: bool,
    timeline: Any = None,
    attacking_direction: int | None = None,
    down: int | None = None,
    distance_yards: float | None = None,
    capture_regime: str = "unknown",
) -> dict[str, Any]:
    spec = definitions(task)
    if timeline is not None and timeline.verified is not True:
        raise DataError("Source PTS timeline is unverified")
    if not analytics_safe:
        raise DataError("Field mapping is not analytics-safe")
    aliases = (
        {"pass_release", "forward_pass_release", "throw"} if task == "completion" else {spec.clock}
    )
    if any(
        e.get("event_type") == "prediction_clock_review_required"
        and (e.get("attributes") or {}).get("clock_family") in aliases
        for e in events
    ):
        raise DataError("Staff prediction clock correction remains unresolved")
    candidates = [e for e in events if e.get("event_type") in aliases and qualified(e)]
    reviewed = [
        e
        for e in candidates
        if (
            e.get("source")
            or (e.get("provenance") or {}).get("source")
            or (e.get("attributes") or {}).get("source")
        )
        in {"human", "manual"}
    ]
    if reviewed:
        candidates = reviewed
    if len(candidates) != 1:
        raise DataError("Missing or ambiguous qualified prediction-clock event")
    event = candidates[0]
    _real_observation(event)
    when = event_time(event, timeline)
    if when is None:
        raise DataError("Missing source PTS observation clock")
    attrs = event.get("attributes") or {}
    if any(
        attrs.get(key, "recording") != "recording"
        for key in (
            "feature_context_timebase",
            "call_context_timebase",
        )
    ):
        raise DataError("Feature/call context clocks require recording timebase")
    shot = event_shot(event)
    if not known_shot(shot):
        raise DataError("Prediction-clock event requires known source shot")
    available = finite(attrs.get("feature_context_available_at_seconds"))
    if not 0 <= available <= when:
        raise DataError("Feature context was unavailable at prediction time")
    if attrs.get("play_design") != ("rush" if task == "rushing_gain" else "pass"):
        raise DataError("Eligible play design is unresolved at prediction time")
    if task in {"completion", "yac"} and (
        attrs.get("forward_pass") is not True
        or attrs.get("pass_direction") == "backward"
        or attrs.get("interception") is True
        or attrs.get("throwaway") is True
    ):
        raise DataError("Aimed forward-pass eligibility is unresolved or contradictory")
    if task == "yac" and (
        attrs.get("completion_observed") is not True or attrs.get("possession_side") != "offense"
    ):
        raise DataError("YAC requires an observed forward completion to the offense at catch")
    tracks = {str(t["id"]): t for t in tracklets if t.get("id") is not None}
    if len(tracks) != len(tracklets):
        raise DataError("Missing or duplicate track identities")
    used = []
    subject = {"scope": "team", "reason": "contextual_front_prediction"}
    if task == "pressure":
        attacking_direction = (
            attrs.get("attacking_direction") if attacking_direction is None else attacking_direction
        )
        down = attrs.get("down") if down is None else down
        distance_yards = attrs.get("distance_yards") if distance_yards is None else distance_yards
        values, used = _pressure_values(
            tracks, event, when, shot, attrs, attacking_direction, down, distance_yards
        )
    else:
        receiver_key = "intended_receiver_id" if task == "completion" else "possessor_id"
        identity = attrs.get(receiver_key)
        player = tracks.get(str(identity)) if identity is not None else None
        if player is None:
            raise DataError("Explicit target/possessor missing at prediction time")
        if player.get("side_of_ball") == "defense" or attrs.get("possession_side") == "defense":
            raise DataError("Target/possessor contradicts offensive eligibility")
        current = _point(player, when)
        if current["shot_id"] != shot:
            raise DataError("Player and prediction event are in different shots")
        used.append({**player, "track_points": [current]})
        ids = attrs.get("defender_track_ids")
        if (
            attrs.get("defender_population_complete") is not True
            or not isinstance(ids, list)
            or not ids
            or len(set(ids)) != len(ids)
            or str(identity) in {str(i) for i in ids}
        ):
            raise DataError("Complete qualified defender population required")
        defenders = []
        for identity in ids:
            track = tracks.get(str(identity))
            if track is None:
                raise DataError("Defender track missing")
            known_team = player.get("team_label") not in (None, "", "unknown", "unresolved")
            if track.get("side_of_ball") == "offense" or (
                known_team and track.get("team_label") == player.get("team_label")
            ):
                raise DataError("Defender identity contradicts known offensive team evidence")
            point = _point(track, when)
            if point.get("shot_id") != current.get("shot_id"):
                raise DataError("Defender and player are in different shots")
            defenders.append(point)
            used.append({**track, "track_points": [point]})
        values = {"nearest_defender_distance": min(_distance(current, p) for p in defenders)}
        if task == "completion":
            qb = tracks.get(str(attrs.get("qb_id")))
            if qb is None:
                raise DataError("Explicit QB missing at release")
            qb_point = _point(qb, when)
            if qb_point.get("shot_id") != current.get("shot_id"):
                raise DataError("QB and target are in different shots")
            values["target_distance_to_qb"] = _distance(current, qb_point)
            used.append({**qb, "track_points": [qb_point]})
            subject = {"scope": "player", "role": "QB", "tracklet_id": str(qb["id"])}
        else:
            before = [p for p in samples(player) if p["timestamp_seconds"] < when]
            if (
                not before
                or not connected(before[-1], current)
                or before[-1].get("visibility_state") not in {"observed", "visible"}
            ):
                raise DataError("No contiguous past sample for runner speed")
            previous = before[-1]
            _real_observation(previous)
            values["runner_speed"] = _distance(previous, current) / (
                when - previous["timestamp_seconds"]
            )
            used[0]["track_points"].insert(0, previous)
            subject = {
                "scope": "player",
                "role": "rusher" if task == "rushing_gain" else "receiver",
                "tracklet_id": str(player["id"]),
            }
    # Strip future trajectory and outcomes before hashing the actual feature evidence.
    context_keys = {
        "feature_context_timebase",
        "call_context_timebase",
        "feature_context_available_at_seconds",
        "play_design",
        "intended_receiver_id",
        "qb_id",
        "defender_track_ids",
        "defender_population_complete",
        "possessor_id",
        "front_population_complete",
        "front_track_ids",
        "los_x",
        "down",
        "distance_yards",
        "attacking_direction",
        "ol_roles",
        "defensive_roles",
        "call_context_known",
        "call_context_available_at_seconds",
        "called_rusher_ids",
        "called_blitzer_ids",
        "protection_blocker_ids",
        "dl_gap_alignment",
        "ball_y",
        "forward_pass",
        "pass_direction",
        "completion_observed",
        "possession_side",
        "interception",
        "throwaway",
    }
    evidence = {
        "event": {
            k: event[k]
            for k in ("id", "event_type", "source", "timestamp_seconds", "frame_number", "shot_id")
            if k in event
        },
        "context": {k: v for k, v in attrs.items() if k in context_keys},
        "tracks": [
            {k: t[k] for k in ("id", "source", "provenance", "track_points") if k in t}
            for t in used
        ],
    }
    evidence["event"]["shot_id"] = shot
    evidence["event"]["source"] = (
        event.get("source") or (event.get("provenance") or {}).get("source") or attrs.get("source")
    )
    row = {
        "schema_version": 1,
        "task": task,
        "feature_version": spec.version,
        "eligible_population": spec.population,
        "source": "real",
        "capture_regime": capture_regime,
        "lineage": lineage,
        "subject": subject,
        "observation_clock": {"event": spec.clock, "timestamp_seconds": when},
        "features": {
            name: {
                "value": values[name],
                "unit": unit,
                "qualified": True,
                "observed_at_seconds": when,
            }
            for name, unit in spec.features
        },
    }
    artifact = feature_artifact(row, observations=evidence)
    digest = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    for feature in row["features"].values():
        feature["evidence_sha256"] = digest
    validate_snapshot(row)
    return {**row, "feature_evidence": artifact}
