"""Observed QB durations with explicit coverage and no proximity-as-pressure."""

from __future__ import annotations

from typing import Any

from pipeline.position_evidence import (
    body_bounds,
    continuous_prefix,
    event_shot,
    event_time,
    evidence,
    known_shot,
    number,
    original_front,
    qualified,
    row,
    sample_at,
    trajectory,
)
from pipeline.video_timeline import VideoTimeline


def observed_qb_durations(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    fps: float,
    timeline: VideoTimeline | None = None,
    *,
    attacking_direction: int | None = None,
) -> list[dict[str, Any]]:
    """Require an explicit QB, reviewed snap, and a continuous observed prefix."""
    snaps = [e for e in events if e.get("event_type") == "snap" and qualified(e)]
    if len(snaps) != 1 or fps <= 0:
        return []
    (snap,) = snaps
    start = event_time(snap, timeline)
    if start is None:
        return []
    qb_ids = {
        str((e.get("attributes") or {})["qb_id"])
        for e in events
        if qualified(e) and (e.get("attributes") or {}).get("qb_id") is not None
    }
    quarterbacks = [
        t
        for t in tracklets
        if str(t.get("id")) in qb_ids
        or (t.get("observed_role") == "QB" and t.get("role_resolved") is True)
    ]
    if len(quarterbacks) != 1:
        return []
    qb = quarterbacks[0]
    prefix = continuous_prefix(qb, start)
    if not prefix:
        return []
    observed_end = prefix[-1]["timestamp_seconds"]
    endpoints = [
        (event_time(e, timeline), e)
        for e in events
        if qualified(e)
        and e.get("event_type") in {"throw", "pass_release", "forward_pass_release", "sack"}
        and (e.get("attributes") or {}).get("qb_id", qb["id"]) == qb["id"]
    ]
    endpoints = [(t, e) for t, e in endpoints if t is not None and start <= t <= observed_end]
    end, endpoint = min(endpoints, key=lambda item: item[0]) if endpoints else (observed_end, None)
    if end <= start and endpoint is None:
        return []
    packet = {
        "seconds": round(end - start, 6),
        "right_censored": endpoint is None,
        "endpoint": endpoint["event_type"] if endpoint else "visibility_or_film_end",
        "source_start_seconds": start,
        "source_end_seconds": end,
        "trajectory_source_start_seconds": start,
        "trajectory_source_end_seconds": end,
        "track_id": qb["id"],
        "event_evidence": evidence([snap, *([endpoint] if endpoint else [])], timeline),
        "required_evidence": [
            "resolved_qb",
            "qualified_snap",
            "continuous_visibility",
            "terminal_event_or_censor_time",
        ],
        "trajectory": trajectory(qb, start, end),
    }
    scrambles = [
        (event_time(e, timeline), e)
        for e in events
        if qualified(e)
        and e.get("event_type") == "scramble_start"
        and (e.get("attributes") or {}).get("qb_id", qb["id"]) == qb["id"]
        and event_time(e, timeline) is not None
        and start <= event_time(e, timeline) <= end
    ]
    holding = packet
    if scrambles:
        when, scramble = min(scrambles, key=lambda item: item[0])
        holding = {
            **packet,
            "seconds": round(when - start, 6),
            "source_end_seconds": when,
            "trajectory_source_end_seconds": when,
            "right_censored": False,
            "endpoint": "scramble_start",
            "event_evidence": evidence([snap, scramble], timeline),
            "trajectory": trajectory(qb, start, when),
        }
    metrics = [row("ball_holding_duration", holding, qb["id"])]
    for name, value in (
        ("time_to_first_pressure", _first_pressure(events, qb, packet, timeline)),
        (
            "tackle_box_residence",
            _corridor(tracklets, qb, snap, packet, timeline, attacking_direction),
        ),
        ("rusher_proximity", _proximity(tracklets, qb, packet, events, timeline)),
    ):
        if value:
            metrics.append(row(name, value, qb["id"], "yd" if name == "rusher_proximity" else "s"))
    return metrics


def _first_pressure(events, qb, packet, timeline):
    start, end = packet["source_start_seconds"], packet["source_end_seconds"]
    intervals = []
    for event in events:
        attrs = event.get("attributes") or {}
        a, b = number(attrs.get("start_seconds")), number(attrs.get("end_seconds"))
        if (
            event.get("event_type") != "pressure_observation"
            or not qualified(event)
            or attrs.get("coverage_complete") is not True
            or attrs.get("qb_id") != qb["id"]
            or attrs.get("pressure_state") not in {"none", "pressure"}
            or a is None
            or b is None
            or b <= a
        ):
            continue
        if attrs.get("interval_timebase") not in {None, "recording"}:
            continue
        event_start = event_time(event, timeline)
        if attrs.get("interval_timebase") is None and (
            event_start is None or abs(event_start - a) > 1e-6
        ):
            continue
        if b >= start and a <= end:
            intervals.append((a, b, event))
    cursor = start
    used = []
    for a, b, event in sorted(intervals, key=lambda item: item[0]):
        if a > cursor + 1e-7:
            break
        if b <= cursor or b <= start:
            continue
        used.append(event)
        cursor = min(end, max(cursor, b))
    if cursor <= start:
        return None
    onsets = [
        (max(start, a), e)
        for a, b, e in intervals
        if (e.get("attributes") or {}).get("pressure_state") == "pressure"
        and b > start
        and a <= cursor
        and a < end
    ]
    onsets.extend(
        (event_time(e, timeline), e)
        for e in events
        if e.get("event_type") == "pressure"
        and qualified(e)
        and (e.get("attributes") or {}).get("qb_id", qb["id"]) == qb["id"]
        and event_time(e, timeline) is not None
        and start <= event_time(e, timeline) <= cursor
        and event_time(e, timeline) < end
    )
    onset, pressure = min(onsets, key=lambda item: item[0]) if onsets else (cursor, None)
    coverage = [
        {
            "start_seconds": max(start, a),
            "end_seconds": min(end, b),
            "pressure_state": (e.get("attributes") or {})["pressure_state"],
            "event_id": e.get("id"),
        }
        for a, b, e in intervals
        if a <= cursor and min(end, b) > max(start, a)
    ]
    return {
        **packet,
        "seconds": round(onset - start, 6),
        "source_end_seconds": onset,
        "right_censored": pressure is None,
        "endpoint": "qualified_pressure_onset"
        if pressure
        else (packet["endpoint"] if cursor >= end else "pressure_coverage_gap"),
        "pressure_coverage": coverage,
        "series_interpolation": "none",
        "series": [{"timestamp_seconds": item["start_seconds"], **item} for item in coverage],
        "event_evidence": packet["event_evidence"]
        + evidence([*used, *([pressure] if pressure else [])], timeline),
        "required_evidence": [
            "resolved_qb",
            "qualified_snap",
            "continuous_qualified_pressure_coverage",
            "endpoint_or_censor_time",
        ],
    }


def _corridor(tracklets, qb, snap, packet, timeline, attacking_direction=None):
    front = original_front(tracklets, snap, timeline, attacking_direction)
    if front is None:
        return None
    bodies = (snap.get("attributes") or {}).get("body_extents") or {}
    if not isinstance(bodies, dict):
        return None
    ids = [front["roles"]["LT"], front["roles"]["RT"]]
    bounds = [body_bounds(bodies.get(tid)) for tid in ids]
    if any(b is None for b in bounds):
        return None
    low, high = min(b["min_y"] for b in bounds), max(b["max_y"] for b in bounds)
    start, end = packet["source_start_seconds"], packet["source_end_seconds"]
    pts = continuous_prefix(qb, start, field=True)
    if not pts:
        return None
    end = min(end, pts[-1]["timestamp_seconds"])
    end_point = sample_at(qb, end)
    pts = [p for p in pts if p["timestamp_seconds"] <= end]
    if end_point and (not pts or pts[-1]["timestamp_seconds"] < end):
        pts.append(end_point)
    outside_since, confirmed = None, None
    series = []
    for p in pts:
        inside = (
            low <= p["field_y"] <= high
            and (p["field_x"] - front["los_x"]) * front["attacking_direction"] <= 0
        )
        series.append(
            {
                "timestamp_seconds": p["timestamp_seconds"],
                "inside": inside,
                "field_x": p["field_x"],
                "field_y": p["field_y"],
                "shot_id": p.get("shot_id"),
                "visibility_state": p.get("visibility_state"),
            }
        )
        if inside:
            outside_since = None
        elif outside_since is None:
            outside_since = p["timestamp_seconds"]
        if outside_since is not None and p["timestamp_seconds"] - outside_since >= 0.2 - 1e-8:
            confirmed = p["timestamp_seconds"]
            break
    terminal = outside_since if confirmed is not None else end
    censored = confirmed is None and (
        packet["right_censored"] or end < packet["source_end_seconds"]
    )
    return {
        **packet,
        "seconds": round(terminal - start, 6),
        "source_end_seconds": terminal,
        "right_censored": censored,
        "endpoint": "sustained_exit"
        if confirmed is not None
        else ("visibility_or_film_end" if censored else packet["endpoint"]),
        "exit_started_seconds": outside_since if confirmed is not None else None,
        "exit_confirmed_seconds": confirmed,
        "corridor": {
            "los_x": front["los_x"],
            "left_outer_y": high if front["attacking_direction"] == 1 else low,
            "right_outer_y": low if front["attacking_direction"] == 1 else high,
            "attacking_direction": front["attacking_direction"],
            "initial_tackle_ids": ids,
            "behind_los_unbounded": True,
        },
        "series": series,
        "series_interpolation": "none",
        "required_evidence": [
            "resolved_qb",
            "qualified_snap",
            "original_tackle_roles",
            "measured_tackle_body_extents",
            "qualified_los",
            "attacking_direction",
            "continuous_trajectory",
            "sustained_exit_200ms_or_endpoint",
        ],
    }


def _proximity(tracklets, qb, packet, events=(), timeline=None):
    rushers, participation_evidence = [], []
    for track in tracklets:
        if track.get("role_resolved") is not True:
            continue
        observations = [
            event
            for event in events
            if event.get("event_type") == "position_participation"
            and qualified(event)
            and (event.get("attributes") or {}).get("opportunity_kind") == "pass_rush"
            and str((event.get("attributes") or {}).get("actor_id")) == str(track.get("id"))
        ]
        for observation in observations:
            attrs = observation.get("attributes") or {}
            start, end = event_time(observation, timeline), number(attrs.get("end_seconds"))
            shot = event_shot(observation)
            if (
                track.get("team_label") != "defense"
                or not track.get("observed_role")
                or start is None
                or end is None
                or end <= start
                or not known_shot(shot)
                or attrs.get("opponent_id") not in (None, qb["id"])
            ):
                continue
            rushers.append(
                (
                    track,
                    max(start, packet["source_start_seconds"]),
                    min(end, packet["source_end_seconds"]),
                    shot,
                )
            )
            participation_evidence.append(observation)
        # Preserve legacy explicit RUSHER packets; roster position never
        # supplies this role. New position roles need qualified participation.
        if not observations and track.get("observed_role") == "RUSHER":
            rushers.append(
                (track, packet["source_start_seconds"], packet["source_end_seconds"], None)
            )
    if not rushers:
        return None
    supported = []
    for rusher, start, end, participation_shot in rushers:
        points = continuous_prefix(rusher, start, field=True)
        prefix = []
        for point in points:
            if point.get("visibility_state") != "observed":
                break
            prefix.append(point)
        if prefix and (
            participation_shot is None or participation_shot == prefix[0].get("shot_id")
        ):
            supported.append((rusher, start, min(end, prefix[-1]["timestamp_seconds"])))
    series = []
    for point in continuous_prefix(qb, packet["source_start_seconds"], field=True):
        when = point["timestamp_seconds"]
        if when > packet["source_end_seconds"] or point.get("visibility_state") != "observed":
            break
        distances = []
        for rusher, start, end in supported:
            if not start <= when <= end:
                continue
            p = sample_at(rusher, when)
            if (
                p
                and p.get("visibility_state") == "observed"
                and known_shot(p.get("shot_id"))
                and p.get("shot_id") == point.get("shot_id")
            ):
                distance = (
                    (p["field_x"] - point["field_x"]) ** 2 + (p["field_y"] - point["field_y"]) ** 2
                ) ** 0.5
                distances.append((distance, rusher["id"]))
        if not distances:
            series.append(
                {"timestamp_seconds": when, "yards": None, "visibility_state": "unobserved"}
            )
            continue
        distance, rusher_id = min(distances)
        series.append(
            {
                "timestamp_seconds": when,
                "yards": distance,
                "rusher_id": rusher_id,
                "shot_id": point.get("shot_id"),
                "visibility_state": "observed",
            }
        )
    values = [p["yards"] for p in series if p["yards"] is not None]
    partial = bool(packet["right_censored"]) or len(values) != len(series)
    return (
        {
            **packet,
            "yards": min(values),
            "right_censored": False,
            "partial_observation": partial,
            "measurement_complete": not partial,
            "scalar_scope": "supported_observed_samples" if partial else "observed_qb_interval",
            "scalar_summary": "minimum_observed_rusher_distance_within_scalar_scope",
            "series": series,
            "series_interpolation": "none",
            "comparison_scope": "observed_resolved_rushers_only",
            "opportunity_kind": "pass_rush",
            "event_evidence": packet["event_evidence"] + evidence(participation_evidence, timeline),
            "pressure_inferred": False,
            "required_evidence": [
                "resolved_qb",
                "resolved_rusher_role",
                "calibrated_positions",
                "source_timestamps",
            ],
        }
        if values
        else None
    )
