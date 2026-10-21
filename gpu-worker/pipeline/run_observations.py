"""Carrier-event observations: signed net gain and original-gap LOS entry.

No most-moving-player carrier selection, distance-as-broken-tackle, or implied
intended gap. Unmeasured possession/contact/entry evidence stays unavailable.
"""

from __future__ import annotations

from typing import Any

from pipeline.homography.field_template import FIELD_LENGTH
from pipeline.position_evidence import (
    connected,
    continuous_prefix,
    event_shot,
    event_time,
    evidence,
    named_gaps,
    number,
    original_front,
    qualified,
    row,
    sample_at,
    trajectory,
)
from pipeline.trench_observations import interaction_geometry
from pipeline.video_timeline import VideoTimeline


def observed_runs(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timeline: VideoTimeline | None = None,
    attacking_direction: int | None = None,
) -> list[dict[str, Any]]:
    snaps = [e for e in events if e.get("event_type") == "snap" and qualified(e)]
    if len(snaps) != 1:
        return []
    (snap,) = snaps
    attrs = snap.get("attributes") or {}
    los = number(attrs.get("los_x"))
    direction = attrs.get("attacking_direction", attacking_direction)
    start = event_time(snap, timeline)
    if (
        los is None
        or not 0 <= los <= FIELD_LENGTH
        or direction not in (-1, 1)
        or isinstance(direction, bool)
        or start is None
        or (attacking_direction in (-1, 1) and direction != attacking_direction)
    ):
        return []
    possession = [
        e
        for e in events
        if qualified(e)
        and e.get("event_type") in {"handoff", "possession_start"}
        and event_time(e, timeline) is not None
        and event_time(e, timeline) >= start
    ]
    if any(_ambiguous_carrier(e) for e in possession):
        return []
    identities = {str(_carrier(e)) for e in possession if _carrier(e) is not None}
    if len(identities) != 1:
        return []
    (carrier_id,) = identities
    carrier = next((t for t in tracklets if str(t.get("id")) == carrier_id), None)
    possession = [e for e in possession if str(_carrier(e)) == carrier_id]
    if carrier is None or not possession:
        return []
    handoff = min(possession, key=lambda e: event_time(e, timeline))
    received = event_time(handoff, timeline)
    if any(
        qualified(e)
        and e.get("event_type") in {"end_of_play", "tackle", "out_of_bounds", "touchdown"}
        and _ambiguous_carrier(e)
        and event_time(e, timeline) is not None
        and event_time(e, timeline) >= received
        for e in events
    ):
        return []
    terminal_events = [
        e
        for e in events
        if qualified(e)
        and e.get("event_type") in {"end_of_play", "tackle", "out_of_bounds", "touchdown"}
        and not _ambiguous_carrier(e)
        and (_carrier(e) is None or str(_carrier(e)) == carrier_id)
        and event_time(e, timeline) is not None
        and event_time(e, timeline) >= received
    ]
    prefix = continuous_prefix(carrier, received, field=True)
    if not prefix:
        return []
    terminal_events = [
        e for e in terminal_events if event_time(e, timeline) <= prefix[-1]["timestamp_seconds"]
    ]
    terminal = (
        min(terminal_events, key=lambda e: event_time(e, timeline)) if terminal_events else None
    )
    end = event_time(terminal, timeline) if terminal else prefix[-1]["timestamp_seconds"]
    end_point = sample_at(carrier, end)
    if not prefix or prefix[-1]["timestamp_seconds"] + 1e-8 < end or end_point is None:
        return []
    packet = {
        "track_id": carrier_id,
        "los_x": los,
        "attacking_direction": direction,
        "source_start_seconds": received,
        "source_end_seconds": end,
        "trajectory_source_start_seconds": received,
        "trajectory_source_end_seconds": end,
        "terminal_event": terminal["event_type"] if terminal else None,
        "event_evidence": evidence([snap, handoff, *([terminal] if terminal else [])], timeline),
        "trajectory": trajectory(carrier, received, end),
        "opportunity": "observed_carry",
        "opportunity_event_id": handoff.get("id"),
        "required_evidence": [
            "qualified_snap",
            "qualified_los",
            "attacking_direction",
            "identified_carrier_possession",
            "qualified_terminal_event",
            "continuous_trajectory",
        ],
    }
    gain = {
        **packet,
        "yards": round((end_point["field_x"] - los) * direction, 6),
        "definition_basis": "signed_terminal_position_minus_original_los",
    }
    rows = [row("signed_forward_gain", gain, carrier_id, "yd")] if terminal else []
    front = original_front(tracklets, snap, timeline, attacking_direction)
    crossing = _crossing(prefix, received, end, los, direction)
    if crossing:
        when, y = crossing
        label = _entry_label(front, y) if front else None
        packet["entry"] = {"timestamp_seconds": when, "field_y": y, "gap_label": label}
        entry = {
            **packet,
            "source_end_seconds": when,
            "entry_seconds": when,
            "entry_field_y": y,
            "gap_label": label,
            "intended_gap": None,
            "gap_basis": "original_observed_formation",
        }
        if terminal:
            rows[0]["metric_value"]["entry"] = {
                "timestamp_seconds": when,
                "field_y": y,
                "gap_label": label,
            }
        if handoff["event_type"] == "handoff":
            timing = {
                **entry,
                "seconds": round(when - received, 6),
                "right_censored": False,
                "endpoint": "qualified_los_crossing",
                "required_evidence": [
                    "qualified_handoff",
                    "identified_carrier",
                    "qualified_los",
                    "attacking_direction",
                    "continuous_los_crossing",
                ],
            }
            rows.append(row("handoff_to_los", timing, carrier_id))
        if label:
            entry.update(
                value=1,
                subject_id=label,
                required_evidence=[
                    "identified_carrier",
                    "qualified_snap",
                    "original_lineman_roles",
                    "qualified_los",
                    "attacking_direction",
                    "continuous_los_crossing",
                ],
            )
            rows.append(row("actual_entry_gap", entry, carrier_id, "rep"))
    contacts = [
        e
        for e in events
        if qualified(e)
        and (
            e.get("event_type") == "first_contact"
            or (
                e.get("event_type") == "contact"
                and (e.get("attributes") or {}).get("first_contact") is True
            )
        )
        and str(_carrier(e)) == carrier_id
        and event_time(e, timeline) is not None
        and received <= event_time(e, timeline) <= end
    ]
    if any(
        qualified(e)
        and (
            e.get("event_type") == "first_contact"
            or (
                e.get("event_type") == "contact"
                and (e.get("attributes") or {}).get("first_contact") is True
            )
        )
        and _ambiguous_carrier(e)
        and event_time(e, timeline) is not None
        and received <= event_time(e, timeline) <= end
        for e in events
    ):
        contacts = []
    if len(contacts) != 1 or not (
        event_shot(contacts[0]) is not None
        and event_shot(contacts[0]) == event_shot(handoff) == event_shot(snap)
        and event_shot(contacts[0]) == prefix[0].get("shot_id")
    ):
        contacts = []
    if contacts:
        contact = contacts[0]
        when = event_time(contact, timeline)
        at_contact = sample_at(carrier, when)
        if at_contact:
            contact_packet = {
                **packet,
                "contact_seconds": when,
                "event_evidence": packet["event_evidence"] + evidence([contact], timeline),
                "required_evidence": [*packet["required_evidence"], "qualified_first_contact"],
            }
            rows.append(
                row(
                    "pre_contact_yards",
                    {
                        **contact_packet,
                        "source_start_seconds": start,
                        "source_end_seconds": when,
                        "yards": round((at_contact["field_x"] - los) * direction, 6),
                    },
                    carrier_id,
                    "yd",
                )
            )
            if terminal:
                rows.append(
                    row(
                        "post_contact_yards",
                        {
                            **contact_packet,
                            "source_start_seconds": when,
                            "yards": round(
                                (end_point["field_x"] - at_contact["field_x"]) * direction, 6
                            ),
                        },
                        carrier_id,
                        "yd",
                    )
                )
    # Add source-bound visual evidence without changing the scalar endpoints.
    # The run path may be only a visible prefix even when a contact/entry scalar
    # has its own complete endpoint. Missing clocks never create a contact mark.
    shot = event_shot(handoff)
    path = [p for p in prefix if p["timestamp_seconds"] <= end]
    if path and path[-1]["timestamp_seconds"] < end:
        path.append(end_point)
    native = (
        timeline is not None
        and timeline.verified
        and len([t for t in tracklets if str(t.get("id")) == carrier_id]) == 1
        and all(
            _native_point(p, timeline)
            for p in path
            if p.get("interpolated") is not True and p.get("predicted") is not True
        )
    )
    if (
        native
        and shot is not None
        and event_shot(snap) == shot
        and all(p.get("shot_id") == shot for p in path)
    ):
        complete = (
            terminal is not None
            and event_shot(terminal) == shot
            and not any(p.get("interpolated") is True or p.get("predicted") is True for p in path)
        )
        visual = {
            "actor_id": carrier_id,
            "clock_basis": "verified_native_pts",
            "series_interpolation": "none",
            "possession_event_id": handoff.get("id"),
            "source_start_seconds": received,
            "source_end_seconds": end,
            "partial_observation": not complete,
            "measurement_complete": complete,
            "trajectory": [
                {
                    key: p.get(key)
                    for key in (
                        "timestamp_seconds",
                        "frame_number",
                        "field_x",
                        "field_y",
                        "shot_id",
                        "visibility_state",
                        "interpolated",
                        "predicted",
                    )
                }
                for p in path
            ],
            "event_evidence": evidence(
                [snap, handoff, *([terminal] if complete else [])], timeline
            ),
        }
        original = None
        if front and all(
            p.get("shot_id") == shot
            and _native_point(p, timeline)
            and p.get("visibility_state") in {"observed", "visible"}
            and p.get("predicted") is not True
            and p.get("interpolated") is not True
            for p in front["positions"].values()
        ):
            original = {
                "definition_basis": "original_named_snap_centers",
                "snap_event_id": snap.get("id"),
                "timestamp_seconds": start,
                "shot_id": shot,
                "los_x": los,
                "attacking_direction": direction,
                "roles": {
                    role: {
                        "track_id": front["roles"][role],
                        "field_x": p["field_x"],
                        "field_y": p["field_y"],
                    }
                    for role, p in front["positions"].items()
                },
                "gaps": named_gaps(front),
            }
        mark = None
        if len(contacts) == 1 and event_shot(contacts[0]) == shot:
            contact = contacts[0]
            at_contact = sample_at(carrier, event_time(contact, timeline))
            if at_contact and at_contact.get("shot_id") == shot:
                mark = {
                    **evidence([contact], timeline)[0],
                    "actor_id": carrier_id,
                    "field_x": at_contact["field_x"],
                    "field_y": at_contact["field_y"],
                    "visibility_state": at_contact.get("visibility_state"),
                    "interpolated": at_contact.get("interpolated", False),
                    "explicit_first_contact": True,
                }
        for measured in rows:
            measured["metric_value"].update(
                run_path=visual,
                original_front=original,
                first_contact=mark,
            )
        rows.append(
            row(
                "observed_run_path",
                {
                    **packet,
                    "run_path": visual,
                    "original_front": original,
                    "first_contact": mark,
                    "partial_observation": not complete,
                    "measurement_complete": complete,
                    "required_evidence": [
                        "qualified_snap_los_direction",
                        "identified_carrier_possession",
                        "calibrated_positions",
                        "same_known_shot",
                        "continuous_visible_prefix",
                    ],
                },
                carrier_id,
                "field_trajectory",
            )
        )
    return rows


def _native_point(point, timeline):
    if timeline is None or not timeline.verified:
        return False
    frame, seconds = point.get("frame_number"), number(point.get("timestamp_seconds"))
    return (
        type(frame) is int
        and 0 <= frame < len(timeline.timestamps)
        and seconds is not None
        and abs(timeline.timestamp(frame) - seconds) <= 1e-8
    )


def _carrier(event):
    attrs = event.get("attributes") or {}
    if _ambiguous_carrier(event):
        return None
    return next(
        (
            attrs[key]
            for key in ("carrier_id", "ball_carrier_id", "runner_id")
            if attrs.get(key) is not None
        ),
        None,
    )


def _ambiguous_carrier(event):
    attrs = event.get("attributes") or {}
    values = [
        attrs[key]
        for key in ("carrier_id", "ball_carrier_id", "runner_id")
        if attrs.get(key) is not None
    ]
    return (
        any(not isinstance(value, str) or not value.strip() for value in values)
        or len(set(values)) > 1
    )


def _crossing(points, start, end, los, direction):
    for a, b in zip(points, points[1:]):
        if b["timestamp_seconds"] > end or a["timestamp_seconds"] < start or not connected(a, b):
            continue
        x0, x1 = (a["field_x"] - los) * direction, (b["field_x"] - los) * direction
        if x0 < 0 <= x1:
            ratio = -x0 / (x1 - x0)
            return (
                round(
                    a["timestamp_seconds"]
                    + ratio * (b["timestamp_seconds"] - a["timestamp_seconds"]),
                    6,
                ),
                a["field_y"] + ratio * (b["field_y"] - a["field_y"]),
            )
    return None


def _entry_label(front, lateral):
    for gap in named_gaps(front):
        # Exact centerline of a blocker is ambiguous, not an invented gap.
        if gap["y_min"] < lateral < gap["y_max"]:
            return gap["gap_label"]
    direction = front["attacking_direction"]
    for side, roles, outward in (("left", ("LTE", "LT"), 1), ("right", ("RTE", "RT"), -1)):
        outer = max(
            (role for role in roles if role in front["roles"]),
            key=lambda role: front["positions"][role]["field_y"] * direction * outward,
        )
        if (lateral - front["positions"][outer]["field_y"]) * direction * outward > 0:
            return f"{side}_edge"
    return None


def _interaction_actor(event: dict[str, Any]) -> str | None:
    attrs = event.get("attributes") or {}
    candidates = [attrs[key] for key in ("blocker_id", "player_track_id") if key in attrs]
    if not candidates or any(
        not isinstance(value, str) or not value.strip() for value in candidates
    ):
        return None
    return candidates[0] if len(set(candidates)) == 1 else None

def observed_interactions(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timeline: VideoTimeline | None = None,
    *,
    analytics_safe: bool = False,
) -> list[dict[str, Any]]:
    """Explicit paired interaction IDs; closeness never supplies an event."""
    rows = []
    track_ids = {str(t.get("id")) for t in tracklets}
    for start_type, end_type, name in (
        ("engagement_start", "engagement_end", "engagement_duration"),
        ("chip_end", "route_start", "chip_to_route_delay"),
    ):
        for start in events:
            attrs = start.get("attributes") or {}
            interaction = attrs.get("interaction_id")
            actor = _interaction_actor(start)
            defender = attrs.get("defender_id")
            if (
                start.get("event_type") != start_type
                or not qualified(start)
                or event_shot(start) is None
                or not interaction
                or str(actor) not in track_ids
                or (
                    name == "engagement_duration"
                    and (str(defender) not in track_ids or actor == defender)
                )
            ):
                continue
            if any(
                event.get("event_type") in {start_type, end_type}
                and qualified(event)
                and event_shot(event) == event_shot(start)
                and (event.get("attributes") or {}).get("interaction_id") == interaction
                and (
                    name != "engagement_duration"
                    or (event.get("attributes") or {}).get("defender_id") == defender
                )
                and _interaction_actor(event) is None
                for event in events
            ):
                continue
            starts = [
                event
                for event in events
                if event.get("event_type") == start_type
                and qualified(event)
                and event_shot(event) == event_shot(start)
                and (event.get("attributes") or {}).get("interaction_id") == interaction
                and _interaction_actor(event) == actor
                and (
                    name != "engagement_duration"
                    or (event.get("attributes") or {}).get("defender_id") == defender
                )
            ]
            if len(starts) != 1:
                continue
            first = event_time(start, timeline)
            ends = [
                e
                for e in events
                if e.get("event_type") == end_type
                and qualified(e)
                and event_shot(e) == event_shot(start)
                and (e.get("attributes") or {}).get("interaction_id") == interaction
                and _interaction_actor(e) == actor
                and (
                    name != "engagement_duration"
                    or (e.get("attributes") or {}).get("defender_id") == defender
                )
                and event_time(e, timeline) is not None
                and first is not None
                and event_time(e, timeline) >= first
            ]
            if len(ends) != 1:
                continue
            (end,) = ends
            last = event_time(end, timeline)
            rows.append(
                row(
                    name,
                    {
                        "seconds": round(last - first, 6),
                        "right_censored": False,
                        "source_start_seconds": first,
                        "source_end_seconds": last,
                        "track_id": actor,
                        "interaction_id": interaction,
                        "defender_id": attrs.get("defender_id"),
                        "subject_id": str(interaction),
                        "interaction_geometry": interaction_geometry(
                            tracklets,
                            str(actor),
                            str(defender),
                            first,
                            last,
                            event_shot(start),
                            timeline,
                            analytics_safe=analytics_safe,
                        )
                        if name == "engagement_duration"
                        else None,
                        "event_evidence": evidence([start, end], timeline),
                        "required_evidence": [
                            "qualified_interaction_start",
                            "qualified_interaction_end",
                            "same_interaction_id",
                            "resolved_actor",
                            "source_timestamps",
                            "same_known_shot",
                        ],
                    },
                    actor,
                )
            )
    return rows
