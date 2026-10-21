"""Discrete measured LOS lane changes, with interval/censored transition clocks.

A positive section means width > the explicit analytical threshold (default
zero yards). It does not establish runner fit, a continuous route or success.
No occupancy interpolation or guessed event-time width is permitted.
"""

from __future__ import annotations

from collections import defaultdict

from pipeline.observed_position_products import _path
from pipeline.oline_observations import measured_lanes
from pipeline.position_evidence import event_shot, event_time, evidence, number, qualified, row
from pipeline.run_observations import observed_runs


def _real(packet):
    return all(
        isinstance(layer, dict)
        and not any(
            marker in str(layer.get("source", "")).lower()
            for marker in ("synthetic", "stub", "mock", "test", "placeholder")
        )
        for layer in (packet, packet.get("provenance") or {}, packet.get("attributes") or {})
    )


def _valid(event, timeline):
    when = event_time(event, timeline)
    bodies = (event.get("attributes") or {}).get("body_extents") or {}
    return bool(
        event.get("id")
        and qualified(event)
        and _real(event)
        and event_shot(event) is not None
        and when is not None
        and (
            timeline is None or abs(timeline.timestamp(timeline.frame_for_time(when)) - when) < 1e-6
        )
        and isinstance(bodies, dict)
        and all(_real(body) for body in bodies.values() if isinstance(body, dict))
    )


def lane_windows(
    tracklets, events, timeline=None, attacking_direction=None, *, lanes=None, threshold_yards=0.0
):
    threshold = number(threshold_yards)
    if (
        threshold is None
        or threshold < 0
        or (timeline is not None and timeline.verified is not True)
    ):
        return []
    snaps = [event for event in events if event.get("event_type") == "snap"]
    if len(snaps) != 1 or not _valid(snaps[0], timeline):
        return []
    snap, shot = snaps[0], event_shot(snaps[0])
    start = event_time(snap, timeline)
    by_id = {str(track.get("id")): track for track in tracklets}
    if len(by_id) != len(tracklets):
        return []
    measured = (
        measured_lanes(tracklets, events, timeline, attacking_direction) if lanes is None else lanes
    )
    by_event, unknown = defaultdict(list), False
    for event in events:
        if event.get("event_type") not in {"snap", "geometry_observation"}:
            continue
        when = event_time(event, timeline)
        if when is None:
            unknown = True
        elif when >= start:
            by_event[when].append(event)
    handoffs = [
        event
        for event in events
        if event.get("event_type") == "handoff"
        and _valid(event, timeline)
        and event_shot(event) == shot
        and event_time(event, timeline) >= start
    ]
    handoff = handoffs[0] if len(handoffs) == 1 else None
    carrier_id = None
    if handoff:
        attrs = handoff.get("attributes") or {}
        declared = [
            attrs[key]
            for key in ("carrier_id", "ball_carrier_id", "runner_id", "actor_id")
            if key in attrs
        ]
        if (
            declared
            and all(isinstance(identity, str) and identity for identity in declared)
            and len(set(declared)) == 1
        ):
            carrier_id = declared[0]
        else:
            handoff = None
    carrier = by_id.get(str(carrier_id)) if carrier_id is not None else None
    side = (
        {
            value
            for value in (carrier.get("team_label"), carrier.get("side_of_ball"))
            if value in {"offense", "defense", "special_teams"}
        }
        if carrier
        else set()
    )
    if carrier is not None and (
        not qualified(carrier) or not _real(carrier) or side != {"offense"}
    ):
        carrier = None
    entries = (
        [
            item
            for item in observed_runs(tracklets, events, timeline, attacking_direction)
            if item["metric_name"] == "actual_entry_gap"
            and item.get("tracklet_id") == str(carrier_id)
        ]
        if carrier
        else []
    )
    output = []
    for lane in measured:
        if lane.get("metric_name") != "clear_lane_width":
            continue
        packet = lane["metric_value"]
        if any(
            identity not in by_id
            or not _real(by_id[identity])
            or not _path(by_id[identity], start, start, shot)
            for identity in packet.get("initial_pair", [])
        ):
            continue
        region = packet.get("measurement_region") or {}
        if (
            packet.get("occupancy_complete") is not True
            or packet.get("series_interpolation") != "none"
            or region.get("cross_section") != "LOS"
            or number(region.get("los_x")) is None
            or not 0 <= region["los_x"] <= 100
            or number(region.get("y_min")) is None
            or number(region.get("y_max")) is None
            or not -80 / 3 <= region["y_min"] < region["y_max"] <= 80 / 3
        ):
            continue
        samples = defaultdict(list)
        for sample in packet.get("series", []):
            if number(sample.get("timestamp_seconds")) is not None:
                samples[sample["timestamp_seconds"]].append(sample)
        series, used, openings, closures = [], [snap], [], []
        previous = None
        for when in sorted(set(samples) | set(by_event)):
            observations, values = by_event.get(when, []), samples.get(when, [])
            source = observations[0] if len(observations) == 1 else None
            sample = values[0] if len(values) == 1 else None
            width = number(sample.get("width_yards")) if sample else None
            valid = (
                source is not None
                and _valid(source, timeline)
                and event_shot(source) == shot
                and sample is not None
                and sample.get("event_id") == source["id"]
                and sample.get("visibility_state") == "observed"
                and width is not None
                and width >= 0
                and width <= region["y_max"] - region["y_min"] + 1e-8
            )
            if valid:
                for identity in (source.get("attributes") or {}).get("body_extents", {}):
                    track = by_id.get(str(identity))
                    if (
                        track is None
                        or not _real(track)
                        or any(
                            not _real(point)
                            for point in track.get("track_points", [])
                            if point.get("timestamp_seconds") == when
                        )
                    ):
                        valid = False
                        break
            if not valid:
                series.append(
                    {
                        "timestamp_seconds": when,
                        "width_yards": None,
                        "visibility_state": "unobserved",
                        "break_before": True,
                    }
                )
                if previous is not None and previous["width_yards"] > threshold:
                    closures.append(
                        {
                            "lower_seconds": previous["timestamp_seconds"],
                            "upper_seconds": None,
                            "right_censored": True,
                        }
                    )
                previous = None
                continue
            current = {**sample, "shot_id": shot, "break_before": previous is None}
            series.append(current)
            if source not in used:
                used.append(source)
            opened = width > threshold
            if opened and (previous is None or previous["width_yards"] <= threshold):
                openings.append(
                    {
                        "lower_seconds": previous["timestamp_seconds"] if previous else None,
                        "upper_seconds": when,
                        "left_censored": previous is None,
                    }
                )
            elif not opened and previous is not None and previous["width_yards"] > threshold:
                closures.append(
                    {
                        "lower_seconds": previous["timestamp_seconds"],
                        "upper_seconds": when,
                        "right_censored": False,
                    }
                )
            previous = current
        if previous is not None and previous["width_yards"] > threshold:
            closures.append(
                {
                    "lower_seconds": previous["timestamp_seconds"],
                    "upper_seconds": None,
                    "right_censored": True,
                }
            )
        actual = [sample for sample in series if sample["width_yards"] is not None]
        if not actual:
            continue
        body = {
            **packet,
            "series": series,
            "source_start_seconds": actual[0]["timestamp_seconds"],
            "source_end_seconds": actual[-1]["timestamp_seconds"],
            "snap_seconds": start,
            "opening_threshold_yards": threshold,
            "threshold_operator": ">",
            "threshold_definition": "strictly_positive_clear_LOS_section"
            if threshold == 0
            else "explicit_clear_width_threshold",
            "transition_clock_kind": "interval_censored_between_measured_snapshots",
            "continuous_openness_claimed": False,
            "openings": openings,
            "closures": closures,
            "unknown_clock_observations": int(unknown),
            "event_evidence": evidence(used, timeline),
        }
        if unknown:
            body["openings"], body["closures"] = [], []
        output.append(row("gap_clear_timeline", body, unit="source_interval"))
        alignments = []
        if handoff and carrier:
            alignments.append(("handoff", event_time(handoff, timeline), handoff, None))
        for entry in entries:
            value = entry["metric_value"]
            when = value.get("entry_seconds")
            if value.get("gap_label") == packet.get("gap_label") and number(when) is not None:
                path = _path(carrier, event_time(handoff, timeline), when, shot)
                if (
                    path
                    and abs(path[-1]["timestamp_seconds"] - when) < 1e-8
                    and all(_real(point) for point in path)
                ):
                    alignments.append(("entry", when, handoff, value))
        for kind, when, event, entry in alignments:
            exact = [sample for sample in actual if abs(sample["timestamp_seconds"] - when) < 1e-8]
            if len(exact) != 1:
                continue
            match = exact[0]
            output.append(
                row(
                    f"clear_lane_width_at_{kind}",
                    {
                        **body,
                        "width_yards": match["width_yards"],
                        "alignment_event": kind,
                        "alignment_seconds": when,
                        "alignment_event_id": event["id"],
                        "carrier_id": str(carrier_id),
                        "actual_entry": entry,
                        "event_evidence": evidence([*used, event], timeline),
                        "measurement_event_id": match["event_id"],
                    },
                    str(carrier_id),
                    "yd",
                )
            )
    return output
