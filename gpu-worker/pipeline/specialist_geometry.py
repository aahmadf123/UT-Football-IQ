"""Qualified measured specialist geometry; no protection or assignment grades."""

from __future__ import annotations

import math
from collections import defaultdict

from pipeline.observed_position_products import _geometry_status, _path, _role, _shot, _slim
from pipeline.position_evidence import (
    event_time,
    evidence,
    number,
    original_front,
    qualified,
    row,
    sample_at,
    visible,
)

LINEMEN = {"LT", "LG", "C", "RG", "RT"}


def _field_point(value):
    if not isinstance(value, dict):
        return None
    point = {
        "field_x": number(value.get("field_x")),
        "field_y": number(value.get("field_y")),
        "visibility_state": "observed",
    }
    return point if visible(point, field=True) else None


def _polygon(raw):
    if not isinstance(raw, list) or not 3 <= len(raw) <= 64:
        return None
    pts = [_field_point(point) for point in raw]
    if any(point is None for point in pts):
        return None
    xy = [(point["field_x"], point["field_y"]) for point in pts]
    if xy[-1] == xy[0]:
        xy.pop()
    if len(xy) < 3 or len(set(xy)) != len(xy):
        return None

    def orientation(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on(a, b, c):
        return (
            abs(orientation(a, b, c)) < 1e-9
            and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
        )

    def intersects(a, b, c, d):
        return (
            (
                orientation(a, b, c) * orientation(a, b, d) < 0
                and orientation(c, d, a) * orientation(c, d, b) < 0
            )
            or on(a, b, c)
            or on(a, b, d)
            or on(c, d, a)
            or on(c, d, b)
        )

    edges = list(zip(xy, [*xy[1:], xy[0]]))
    for i, (a, b) in enumerate(edges):
        for j, (c, d) in enumerate(edges):
            if j <= i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            if intersects(a, b, c, d):
                return None
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
    if area <= 1e-8:
        return None
    return {
        "polygon": [{"field_x": x, "field_y": y} for x, y in xy],
        "width_yards": max(y for _, y in xy) - min(y for _, y in xy),
        "depth_yards": max(x for x, _ in xy) - min(x for x, _ in xy),
        "area_square_yards": area,
    }


def _envelopes(events, timeline, by_id):
    groups = defaultdict(list)
    for event in events:
        if event.get("event_type") == "protection_envelope":
            identifier = (event.get("attributes") or {}).get("envelope_id")
            if isinstance(identifier, str) and identifier:
                groups[identifier].append(event)
    rows = []
    for identifier, observations in groups.items():
        if any(event_time(event, timeline) is None for event in observations):
            continue
        observations.sort(key=lambda event: event_time(event, timeline))
        first_attrs = observations[0].get("attributes") or {}
        actor_id = first_attrs.get("actor_id")
        if actor_id is not None and not _role(by_id.get(str(actor_id), {}), {"QB"}, "offense"):
            continue
        start, end = (
            number(first_attrs.get("start_seconds")),
            number(first_attrs.get("end_seconds")),
        )
        shot = _shot(observations[0])
        series, used = [], []
        for event in observations:
            attrs = event.get("attributes") or {}
            when = event_time(event, timeline)
            measured = _polygon(attrs.get("polygon"))
            if (
                not qualified(event)
                or not event.get("id")
                or not shot
                or _shot(event) != shot
                or attrs.get("visibility_state") != "observed"
                or measured is None
                or attrs.get("actor_id") != actor_id
                or (series and not 0 < when - series[-1]["timestamp_seconds"] <= 0.2 + 1e-8)
            ):
                break
            if (number(attrs.get("start_seconds")), number(attrs.get("end_seconds"))) != (
                start,
                end,
            ):
                break
            if start is not None and end is not None and not start <= when <= end:
                break
            series.append(
                {
                    **measured,
                    "timestamp_seconds": when,
                    "shot_id": shot,
                    "visibility_state": "observed",
                    "event_id": event["id"],
                }
            )
            used.append(event)
        if not series:
            continue
        complete = (
            start is not None
            and end is not None
            and start < end
            and abs(series[0]["timestamp_seconds"] - start) < 1e-8
            and abs(series[-1]["timestamp_seconds"] - end) < 1e-8
            and len(used) == len(observations)
        )
        packet = {
            "subject_id": identifier,
            "envelope_id": identifier,
            "actor_id": first_attrs.get("actor_id"),
            "shot_id": shot,
            "visibility_state": "observed",
            "opponent_id": None,
            "definition_basis": "explicit_measured_protection_polygon",
            "source_start_seconds": series[0]["timestamp_seconds"],
            "source_end_seconds": series[-1]["timestamp_seconds"],
            "declared_start_seconds": start,
            "declared_end_seconds": end,
            "right_censored": False,
            "partial_observation": not complete,
            "measurement_complete": complete,
            "scalar_scope": "declared_measurement_window" if complete else "supported_prefix",
            "scalar_summary": "maximum_observed_value_within_scalar_scope",
            "protection_success_inferred": False,
            "series": series,
            "series_interpolation": "none",
            "polygon": series[0]["polygon"],
            "event_evidence": evidence(used, timeline),
            "required_evidence": [
                "qualified_measured_polygon",
                "native_source_timestamps",
                "same_known_shot",
                "continuous_visibility_200ms",
            ],
        }
        for name, key, unit in [
            ("protection_envelope_width", "width_yards", "yd"),
            ("protection_envelope_depth", "depth_yards", "yd"),
            ("protection_envelope_area", "area_square_yards", "yd²"),
        ]:
            rows.append(row(name, {**packet, key: max(point[key] for point in series)}, unit=unit))
        if len(series) > 1:
            rates = [
                {
                    **b,
                    "interval_start_seconds": a["timestamp_seconds"],
                    "square_yards_per_second": (a["area_square_yards"] - b["area_square_yards"])
                    / (b["timestamp_seconds"] - a["timestamp_seconds"]),
                }
                for a, b in zip(series, series[1:])
            ]
            rows.append(
                row(
                    "protection_envelope_compression",
                    {
                        **packet,
                        "series": rates,
                        "scalar_summary": "interval_mean_signed_area_decrease_within_scalar_scope",
                        "square_yards_per_second": (
                            series[0]["area_square_yards"] - series[-1]["area_square_yards"]
                        )
                        / (series[-1]["timestamp_seconds"] - series[0]["timestamp_seconds"]),
                    },
                    unit="yd²/s",
                )
            )
    return rows


def _actor_packet(actor, path, events, timeline, declared_end, kind=None):
    return {
        "actor_id": actor["id"],
        "track_id": actor["id"],
        "actor_role": actor.get("observed_role"),
        "opponent_id": None,
        "role_evidence": actor.get("role_evidence") or [],
        "opportunity_kind": kind,
        "source_start_seconds": path[0]["timestamp_seconds"],
        "source_end_seconds": path[-1]["timestamp_seconds"],
        "trajectory": [_slim(point) for point in path],
        "series_interpolation": "none",
        **_geometry_status(path[-1]["timestamp_seconds"], declared_end),
        "event_evidence": evidence(events, timeline),
        "required_evidence": [
            "resolved_role",
            "explicit_team",
            "qualified_snap_los_direction",
            "calibrated_trajectory",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
    }


def _receiver_locations(tracklets, events, timeline):
    rows = []
    for actor in tracklets:
        if not _role(actor, {"WR", "TE"}, "offense"):
            continue
        for participation in events:
            attrs = participation.get("attributes") or {}
            start, end = event_time(participation, timeline), number(attrs.get("end_seconds"))
            shot = _shot(participation)
            if (
                participation.get("event_type") != "position_participation"
                or not qualified(participation)
                or str(attrs.get("actor_id")) != str(actor["id"])
                or attrs.get("opportunity_kind") != "routes"
                or start is None
                or end is None
                or end <= start
                or not shot
            ):
                continue
            for location in events:
                location_attrs = location.get("attributes") or {}
                when = event_time(location, timeline)
                if (
                    location.get("event_type") not in {"receiver_release", "route_break"}
                    or not qualified(location)
                    or not location.get("id")
                    or str(location_attrs.get("actor_id")) != str(actor["id"])
                    or when is None
                    or not start <= when <= end
                    or _shot(location) != shot
                    or location_attrs.get("visibility_state") not in (None, "observed")
                ):
                    continue
                point = _field_point(location_attrs)
                support = _path(actor, when, when, shot)
                if not point or not support:
                    continue
                rows.append(
                    row(
                        "receiver_release_location"
                        if location["event_type"] == "receiver_release"
                        else "receiver_break_location",
                        {
                            **_actor_packet(
                                actor, support, [participation, location], timeline, when, "routes"
                            ),
                            **point,
                            "subject_id": location["id"],
                            "timestamp_seconds": when,
                            "shot_id": shot,
                            "location_event_id": location["id"],
                            "source_start_seconds": when,
                            "source_end_seconds": when,
                            "series": [
                                {
                                    **point,
                                    "timestamp_seconds": when,
                                    "shot_id": shot,
                                    "event_id": location["id"],
                                }
                            ],
                            "trajectory": [{**_slim(support[0]), **point}],
                            "definition_basis": "explicit_qualified_actor_location",
                            "required_evidence": [
                                "resolved_receiver_role",
                                "qualified_routes_participation",
                                "exact_actor_event_id",
                                "calibrated_location",
                                "native_source_timestamps",
                                "same_known_shot",
                            ],
                        },
                        actor["id"],
                        "field_location",
                    )
                )
    return rows


def specialist_geometry(
    tracklets, events, timeline=None, *, analytics_safe=True, attacking_direction=None
):
    """Measured polygons, named-role geometry and explicit actor point events."""
    if not analytics_safe or (timeline is not None and not timeline.verified):
        return []
    by_id = {str(track.get("id")): track for track in tracklets}
    if any(not track.get("id") for track in tracklets) or len(by_id) != len(tracklets):
        return []
    rows = _envelopes(events, timeline, by_id)
    rows.extend(_receiver_locations(tracklets, events, timeline))
    snaps = [event for event in events if event.get("event_type") == "snap" and qualified(event)]
    if len(snaps) != 1:
        return rows
    snap = snaps[0]
    attrs = snap.get("attributes") or {}
    start, los = event_time(snap, timeline), number(attrs.get("los_x"))
    direction = attrs.get("attacking_direction", attacking_direction)
    shot = _shot(snap)
    if (
        start is None
        or los is None
        or not 0 <= los <= 100
        or isinstance(direction, bool)
        or direction not in (-1, 1)
        or not shot
        or (attacking_direction in (-1, 1) and attacking_direction != direction)
    ):
        return rows
    front = original_front(tracklets, snap, timeline, direction)
    participations = [
        event
        for event in events
        if event.get("event_type") == "position_participation" and qualified(event)
    ]
    if front and all(
        point.get("shot_id") == shot and point.get("visibility_state") == "observed"
        for point in front["positions"].values()
    ):
        for role, tid in front["roles"].items():
            actor = by_id.get(str(tid), {})
            if role not in LINEMEN or actor.get("team_label") != "offense":
                continue
            path = _path(actor, start, start + 1, shot)
            if len(path) < 2:
                continue
            first, last = path[0], path[-1]
            packet = {
                **_actor_packet(actor, path, [snap], timeline, start + 1),
                "original_role": role,
                "attacking_direction": direction,
                "los_x": los,
                "yards": math.hypot(
                    last["field_x"] - first["field_x"], last["field_y"] - first["field_y"]
                ),
                "forward_yards": (last["field_x"] - first["field_x"]) * direction,
                "lateral_yards": (last["field_y"] - first["field_y"]) * direction,
                "scalar_summary": "net_displacement_within_scalar_scope",
                "series": [
                    {
                        **_slim(point),
                        "forward_yards": (point["field_x"] - first["field_x"]) * direction,
                        "lateral_yards": (point["field_y"] - first["field_y"]) * direction,
                    }
                    for point in path
                ],
            }
            rows.append(row("lineman_first_second_displacement", packet, tid, "yd"))
            for participation in participations:
                p = participation.get("attributes") or {}
                if (
                    str(p.get("actor_id")) != str(tid)
                    or p.get("opportunity_kind") != "pass_protection"
                    or _shot(participation) != shot
                    or (
                        p.get("attacking_direction") in (-1, 1)
                        and p.get("attacking_direction") != direction
                    )
                ):
                    continue
                p_start, p_end = event_time(participation, timeline), number(p.get("end_seconds"))
                if p_start is None or p_start > start or p_end is None or p_end <= start:
                    continue
                supported = _path(actor, start, min(start + 1, p_end), shot)
                if len(supported) < 2:
                    continue
                end = supported[-1]
                rows.append(
                    row(
                        "observed_pass_set_geometry",
                        {
                            **packet,
                            **_actor_packet(
                                actor,
                                supported,
                                [snap, participation],
                                timeline,
                                start + 1,
                                "pass_protection",
                            ),
                            "participation_event_id": participation["id"],
                            "depth_yards": (los - end["field_x"]) * direction,
                            "forward_yards": (end["field_x"] - first["field_x"]) * direction,
                            "lateral_yards": (end["field_y"] - first["field_y"]) * direction,
                            "series": [
                                {
                                    **_slim(point),
                                    "depth_yards": (los - point["field_x"]) * direction,
                                }
                                for point in supported
                            ],
                            "protection_success_inferred": False,
                        },
                        tid,
                        "yd",
                    )
                )
    centers = [track for track in tracklets if _role(track, {"C"}, "offense")]
    explicit_center = attrs.get("center_id") or (attrs.get("ol_roles") or {}).get("C")
    center = (
        by_id.get(str(explicit_center))
        if explicit_center is not None
        else centers[0]
        if len(centers) == 1
        else None
    )
    if center and not _role(center, {"C"}, "offense"):
        center = None
    if (
        center
        and centers
        and any(str(candidate.get("id")) != str(center["id"]) for candidate in centers)
    ):
        center = None
    center_point = sample_at(center or {}, start)
    for actor in tracklets:
        if not _role(actor, {"WR", "TE"}, "offense"):
            continue
        at_snap = sample_at(actor, start)
        if (
            at_snap
            and center_point
            and at_snap.get("shot_id") == shot == center_point.get("shot_id")
            and at_snap.get("visibility_state") == "observed"
            and center_point.get("visibility_state") == "observed"
            and center
            and center.get("team_label") == "offense"
        ):
            rows.append(
                row(
                    "receiver_snap_alignment",
                    {
                        **_actor_packet(actor, [at_snap], [snap], timeline, start),
                        "attacking_direction": direction,
                        "los_x": los,
                        "center_id": center["id"],
                        "lateral_yards": (at_snap["field_y"] - center_point["field_y"]) * direction,
                        "behind_los_yards": (los - at_snap["field_x"]) * direction,
                        "alignment_inferred": False,
                        "scalar_summary": "signed_snap_split_from_original_center",
                        "series": [_slim(at_snap)],
                    },
                    actor["id"],
                    "yd",
                )
            )
        for participation in participations:
            p = participation.get("attributes") or {}
            first, last = event_time(participation, timeline), number(p.get("end_seconds"))
            if (
                str(p.get("actor_id")) != str(actor["id"])
                or p.get("opportunity_kind") != "routes"
                or first is None
                or last is None
                or first < start
                or last <= first
                or _shot(participation) != shot
                or (
                    p.get("attacking_direction") in (-1, 1)
                    and p.get("attacking_direction") != direction
                )
            ):
                continue
            path = _path(actor, first, last, shot)
            if len(path) < 2:
                continue
            series = [
                {**_slim(point), "yards": (point["field_x"] - los) * direction} for point in path
            ]
            rows.append(
                row(
                    "observed_route_depth",
                    {
                        **_actor_packet(
                            actor, path, [snap, participation], timeline, last, "routes"
                        ),
                        "participation_event_id": participation["id"],
                        "attacking_direction": direction,
                        "los_x": los,
                        "yards": max(point["yards"] for point in series),
                        "series": series,
                        "scalar_summary": "maximum_signed_forward_depth_within_scalar_scope",
                    },
                    actor["id"],
                    "yd",
                )
            )
    return rows
