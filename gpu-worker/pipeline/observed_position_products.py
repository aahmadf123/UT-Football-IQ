"""Explicit position participation and measured paths, never assignments.

An observation needs a qualified ``position_participation`` event with actor,
opportunity kind, end_seconds, shot_id and measured attacking direction. Roles
alone never create routes, pass rushes, pursuits, coverage reps or kicking reps.
All timestamps are recording PTS; frame/fps arithmetic is never a fallback.
"""

from __future__ import annotations

import math
from typing import Any

from pipeline.position_evidence import (
    continuous_prefix,
    event_shot,
    event_time,
    evidence,
    number,
    qualified,
    row,
    sample_at,
    visible,
)
from pipeline.video_timeline import VideoTimeline

DEFENSE_ROLES = {
    "DL",
    "DT",
    "DE",
    "EDGE",
    "LB",
    "ILB",
    "OLB",
    "DB",
    "CB",
    "S",
    "FS",
    "SS",
    "RUSHER",
}
RECEIVER_ROLES = {"WR", "TE"}


def hydrate_position_roles(tracklets, events, labels=()):
    """Hydrate play-local roles from qualified observations, never roster roles.

    Existing explicit resolved packets remain compatible. A staff
    ``position_role`` event (or participation declaring the same fields) carries
    actor_id, observed_role and team_label. A reviewed ``position_roles`` clip
    annotation carries those fields in ``label_value.assignments``. Conflicting
    current observations remain unknown. Player identity is never filled here.
    """
    allowed = {
        "offense": {"QB", "RB", "HB", "FB", "WR", "TE", "LT", "LG", "C", "RG", "RT", "LTE", "RTE"},
        "defense": DEFENSE_ROLES,
        "special_teams": {"K", "P", "BALL", "LS", "HOLDER", "KR", "PR"},
    }
    assignments = {}
    for event in events:
        if (
            event.get("event_type") not in {"position_role", "position_participation", "snap"}
            or not qualified(event)
            or not event.get("id")
            or event_time(event) is None
            or not _shot(event)
        ):
            continue
        attrs = event.get("attributes") or {}
        supplied = [attrs]
        if event.get("event_type") == "snap":
            supplied = [
                {"actor_id": tid, "observed_role": role, "team_label": "offense"}
                for role, tid in (
                    attrs["ol_roles"] if isinstance(attrs.get("ol_roles"), dict) else {}
                ).items()
                if role in {"LT", "LG", "C", "RG", "RT", "LTE", "RTE"}
            ]
            if attrs.get("qb_id") is not None:
                supplied.append(
                    {"actor_id": attrs["qb_id"], "observed_role": "QB", "team_label": "offense"}
                )
        for declaration in supplied:
            assignments.setdefault(str(declaration.get("actor_id")), []).append(
                (
                    declaration,
                    {
                        **evidence([event])[0],
                        "observed_role": declaration.get("observed_role"),
                        "team_label": declaration.get("team_label"),
                    },
                )
            )
    for label in labels:
        if label.get("label_type") != "position_roles" or not qualified(label):
            continue
        for attrs in (label.get("label_value") or {}).get("assignments") or []:
            if not isinstance(attrs, dict):
                continue
            assignments.setdefault(str(attrs.get("actor_id")), []).append(
                (
                    attrs,
                    {
                        "label_id": label.get("id"),
                        "label_type": "position_roles",
                        "source": label.get("source"),
                        "actor_id": attrs.get("actor_id"),
                        "observed_role": attrs.get("observed_role"),
                        "team_label": attrs.get("team_label"),
                    },
                )
            )
    result = []
    for original in tracklets:
        track = dict(original)
        observations = [
            (attrs, proof)
            for attrs, proof in assignments.get(str(track.get("id")), [])
            if isinstance(attrs.get("team_label"), str)
            and isinstance(attrs.get("observed_role"), str)
            and attrs["observed_role"] in allowed.get(attrs["team_label"], set())
        ]
        roles = {(attrs["observed_role"], attrs["team_label"]) for attrs, _proof in observations}
        if len(roles) > 1:
            track.update(
                observed_role=None,
                role_resolved=False,
                role_evidence=[proof for _attrs, proof in observations],
                role_resolution_reason="conflicting_qualified_role_observations",
            )
        elif roles:
            role, team = next(iter(roles))
            track.update(
                observed_role=role,
                role_resolved=True,
                team_label=team,
                role_evidence=[proof for _attrs, proof in observations],
            )
        result.append(track)
    return result


def _shot(event):
    return event_shot(event)


def _role(track, roles, team):
    return (
        track.get("role_resolved") is True
        and track.get("observed_role") in roles
        and track.get("team_label") == team
    )


def _path(track, start, end, shot, *, field=True):
    if not shot:
        return []
    # Unknown visibility must not inherit the generic helper's permissive
    # legacy missing-state behavior. Preserve loss markers so interpolation
    # cannot step around an unknown sample.
    strict = {
        **track,
        "track_points": [
            p
            if p.get("visibility_state") == "observed"
            else {**p, "visibility_state": "unobserved"}
            for p in track.get("track_points", [])
        ],
    }
    points = continuous_prefix(strict, start, field=field)
    if not points or points[0].get("shot_id") != shot:
        return []
    result = [p for p in points if p["timestamp_seconds"] <= end]
    if points[-1]["timestamp_seconds"] >= end:
        endpoint = sample_at(strict, end, field=field)
        if endpoint and endpoint.get("shot_id") == shot and result[-1]["timestamp_seconds"] < end:
            result.append(endpoint)
    return result


def _slim(point):
    return {
        key: point.get(key)
        for key in (
            "timestamp_seconds",
            "frame_number",
            "field_x",
            "field_y",
            "shot_id",
            "visibility_state",
        )
    }


def _distance(a, b):
    return math.hypot(a["field_x"] - b["field_x"], a["field_y"] - b["field_y"])


def _geometry_status(observed_end, declared_end):
    partial = observed_end < declared_end - 1e-8
    return {
        "right_censored": False,
        "partial_observation": partial,
        "measurement_complete": not partial,
        "scalar_scope": "supported_prefix" if partial else "declared_participation_interval",
    }


def _packet(actor, participation, path, direction, timeline):
    attrs = participation["attributes"]
    end = path[-1]["timestamp_seconds"]
    return {
        "track_id": actor["id"],
        "actor_id": actor["id"],
        "actor_role": actor.get("observed_role"),
        "team_label": actor.get("team_label"),
        "role_evidence": actor.get("role_evidence") or [],
        "opponent_id": attrs.get("opponent_id"),
        "opportunity_kind": attrs["opportunity_kind"],
        "participation_event_id": participation["id"],
        "opportunity_observation": True,
        "subject_id": participation["id"],
        "source_start_seconds": path[0]["timestamp_seconds"],
        "source_end_seconds": end,
        **_geometry_status(end, attrs["end_seconds"]),
        "attacking_direction": direction,
        "trajectory": [_slim(p) for p in path],
        "series_interpolation": "none",
        "event_evidence": evidence([participation], timeline),
        "required_evidence": [
            "qualified_position_participation",
            "resolved_role",
            "explicit_team",
            "calibrated_positions",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
            "measured_attacking_direction",
        ],
    }


def observed_position_products(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timeline: VideoTimeline | None = None,
    *,
    attacking_direction: int | None = None,
    analytics_safe: bool = True,
) -> list[dict[str, Any]]:
    """Return experimental packets; absent or hidden evidence stays unknown."""
    if timeline is not None and not timeline.verified:
        return []
    by_id = {str(t.get("id")): t for t in tracklets}
    if any(not t.get("id") for t in tracklets) or len(by_id) != len(tracklets):
        return []
    rows = []
    for participation in events:
        attrs = participation.get("attributes") or {}
        if (
            participation.get("event_type") != "position_participation"
            or not qualified(participation)
            or not participation.get("id")
        ):
            continue
        actor = by_id.get(str(attrs.get("actor_id")))
        start, end = event_time(participation, timeline), number(attrs.get("end_seconds"))
        direction = attrs.get("attacking_direction", attacking_direction)
        if not actor or start is None:
            continue
        kind = attrs.get("opportunity_kind")
        receiver = _role(actor, RECEIVER_ROLES, "offense") and kind == "routes"
        defender = _role(actor, DEFENSE_ROLES, "defense") and kind in {
            "pass_rush",
            "pursuit",
            "coverage",
        }
        kicker = (_role(actor, {"P"}, "special_teams") and kind == "punts") or (
            _role(actor, {"K"}, "special_teams") and kind in {"kickoffs", "field_goal"}
        )
        te_block = _role(actor, {"TE"}, "offense") and kind == "blocking_reps"
        extra_opportunity = (
            kind == "rushes"
            and _role(actor, {"QB", "RB", "HB", "FB", "WR", "TE"}, "offense")
            or kind == "targets"
            and _role(actor, {"RB", "HB", "FB", "WR", "TE"}, "offense")
            or kind == "pass_protection"
            and _role(actor, {"LT", "LG", "C", "RG", "RT", "TE", "RB", "HB", "FB"}, "offense")
            or kind == "tackle_opportunities"
            and _role(actor, DEFENSE_ROLES, "defense")
            or kind == "returns"
            and _role(actor, {"KR", "PR"}, "special_teams")
        )
        if not (receiver or defender or kicker or te_block or extra_opportunity):
            continue
        # Denominators describe explicit participation, including reps whose
        # outcome or geometry is unavailable. Never select only measured reps.
        identity = (actor.get("provenance") or {}).get("identity") or {}
        unresolved = {"unknown", "unresolved", "candidate", "pending", "rejected", "conflict"}
        if (
            actor.get("player_id")
            and actor.get("identity_state") not in unresolved
            and identity.get("status") not in unresolved
            and actor.get("identity_verified") is not False
            and identity.get("approved") is not False
        ):
            rows.append(
                row(
                    "position_opportunity",
                    {
                        "value": 1,
                        "player_id": str(actor["player_id"]),
                        "track_id": actor["id"],
                        "actor_id": actor["id"],
                        "actor_role": actor.get("observed_role"),
                        "team_label": actor.get("team_label"),
                        "role_evidence": actor.get("role_evidence") or [],
                        "opportunity_kind": kind,
                        "participation_event_id": participation["id"],
                        "subject_id": participation["id"],
                        "source_start_seconds": start,
                        "source_end_seconds": end if end is not None and end >= start else None,
                        "canonical_play_id": attrs.get("canonical_play_id"),
                        "opportunity_observation": True,
                        "measurement": "qualified_participation_event",
                        "event_evidence": evidence([participation], timeline),
                        "required_evidence": [
                            "qualified_position_participation",
                            "resolved_player",
                            "resolved_role",
                            "explicit_team",
                            "native_source_timestamps",
                        ],
                    },
                    actor["id"],
                    "rep",
                )
            )
        if (
            not analytics_safe
            or end is None
            or end <= start
            or isinstance(direction, bool)
            or direction not in (-1, 1)
            or (attacking_direction in (-1, 1) and direction != attacking_direction)
        ):
            if kicker and end is not None and end > start:
                rows.extend(
                    _special_teams(
                        actor,
                        participation,
                        _temporal_packet(actor, participation, start, end, timeline),
                        events,
                        by_id,
                        timeline,
                        landing_safe=False,
                    )
                )
            continue
        if kicker:
            rows.extend(
                _special_teams(
                    actor,
                    participation,
                    _temporal_packet(actor, participation, start, end, timeline),
                    events,
                    by_id,
                    timeline,
                    landing_safe=True,
                )
            )
            continue
        path = _path(actor, start, end, _shot(participation))
        if len(path) < 2:
            continue
        packet = _packet(actor, participation, path, direction, timeline)
        if receiver or (defender and kind == "coverage"):
            opponents = _opponents(actor, tracklets, attrs.get("opponent_id"), receiver)
            paired = _paired(actor, path, opponents)
            if paired:
                series, trajectories = paired
                rows.append(
                    row(
                        "observed_separation",
                        {
                            **packet,
                            "yards": min(p["yards"] for p in series),
                            "scalar_summary": "minimum_observed_distance",
                            "source_end_seconds": series[-1]["timestamp_seconds"],
                            **_geometry_status(series[-1]["timestamp_seconds"], end),
                            "trajectory": trajectories[str(actor["id"])],
                            "series": series,
                            "paired_trajectories": trajectories,
                            "comparison_scope": "explicit_comparison_defender"
                            if receiver and attrs.get("opponent_id")
                            else "explicit_comparison_receiver"
                            if not receiver
                            else "nearest_observed_defender",
                            "coverage_responsibility_inferred": False,
                        },
                        actor["id"],
                        "yd",
                    )
                )
        if defender:
            if kind in {"pass_rush", "pursuit"}:
                rows.append(
                    row(
                        "observed_rush_path" if kind == "pass_rush" else "observed_pursuit_path",
                        {
                            **packet,
                            "yards": sum(_distance(a, b) for a, b in zip(path, path[1:])),
                            "scalar_summary": "observed_path_length_within_scalar_scope",
                            "series": [_slim(p) for p in path],
                            "assignment_correctness_inferred": False,
                        },
                        actor["id"],
                        "yd",
                    )
                )
            rows.extend(_first_movement(actor, participation, packet, path, events, timeline))
            opponent = by_id.get(str(attrs.get("opponent_id")))
            if opponent and _role(opponent, {"QB", "RB", "WR", "TE"}, "offense"):
                paired = _paired(actor, path, [opponent])
                if paired:
                    series, trajectories = paired
                    rates = [
                        {
                            **last,
                            "interval_start_seconds": first["timestamp_seconds"],
                            "yards_per_second": (first["yards"] - last["yards"])
                            / (last["timestamp_seconds"] - first["timestamp_seconds"]),
                        }
                        for first, last in zip(series, series[1:])
                    ]
                    if rates:
                        rows.append(
                            row(
                                "observed_closing_speed",
                                {
                                    **packet,
                                    "yards_per_second": (series[0]["yards"] - series[-1]["yards"])
                                    / (
                                        series[-1]["timestamp_seconds"]
                                        - series[0]["timestamp_seconds"]
                                    ),
                                    "scalar_summary": "interval_mean_signed_distance_derivative",
                                    "source_end_seconds": series[-1]["timestamp_seconds"],
                                    **_geometry_status(series[-1]["timestamp_seconds"], end),
                                    "trajectory": trajectories[str(actor["id"])],
                                    "series": rates,
                                    "paired_trajectories": trajectories,
                                    "quality_inferred": False,
                                },
                                actor["id"],
                                "yd/s",
                            )
                        )
        if actor.get("observed_role") == "TE":
            rows.extend(_te_transitions(actor, participation, packet, events, by_id, timeline))
    return rows


def _opponents(actor, tracks, explicit, receiver):
    if explicit is None and not receiver:
        return []
    return [
        t
        for t in tracks
        if t.get("id") != actor.get("id")
        and (explicit is None or str(t.get("id")) == str(explicit))
        and _role(
            t, DEFENSE_ROLES if receiver else RECEIVER_ROLES, "defense" if receiver else "offense"
        )
    ]


def _paired(actor, path, opponents):
    """Stop a pair at its first loss; never restart or bridge a hidden sample."""
    paths = {
        str(t["id"]): _path(
            t, path[0]["timestamp_seconds"], path[-1]["timestamp_seconds"], path[0]["shot_id"]
        )
        for t in opponents
    }
    series, trajectories = [], {str(actor["id"]): []}
    for point in path:
        when = point["timestamp_seconds"]
        candidates = []
        for opponent in opponents:
            tid = str(opponent["id"])
            pts = paths[tid]
            if not pts or when > pts[-1]["timestamp_seconds"] + 1e-8:
                continue
            other = sample_at(opponent, when)
            if other and other.get("shot_id") == point.get("shot_id"):
                candidates.append((_distance(point, other), tid, other))
        if not candidates:
            break
        distance, tid, other = min(candidates, key=lambda item: (item[0], item[1]))
        series.append(
            {
                "timestamp_seconds": when,
                "yards": distance,
                "actor_id": actor["id"],
                "opponent_id": tid,
                "shot_id": point["shot_id"],
                "visibility_state": "observed",
            }
        )
        trajectories[str(actor["id"])].append(_slim(point))
        trajectories.setdefault(tid, []).append(_slim(other))
    return (series, trajectories) if len(series) >= 2 else None


def _event_matches(event, actor, packet, timeline):
    when = event_time(event, timeline)
    return (
        qualified(event)
        and bool(event.get("id"))
        and str((event.get("attributes") or {}).get("actor_id")) == str(actor["id"])
        and bool(packet.get("shot_id") or packet.get("trajectory"))
        and _shot(event) == (packet.get("shot_id") or packet["trajectory"][0]["shot_id"])
        and when is not None
        and packet["source_start_seconds"] <= when <= packet["source_end_seconds"]
    )


def _first_movement(actor, participation, packet, path, events, timeline):
    snaps = [
        e
        for e in events
        if e.get("event_type") == "snap"
        and qualified(e)
        and _shot(e) == _shot(participation)
        and event_time(e, timeline) == packet["source_start_seconds"]
    ]
    movements = [
        e
        for e in events
        if e.get("event_type") == "first_movement" and _event_matches(e, actor, packet, timeline)
    ]
    if len(snaps) != 1 or len(movements) != 1:
        return []
    movement = movements[0]
    when = event_time(movement, timeline)
    return [
        row(
            "get_off_time",
            {
                **packet,
                "seconds": when - packet["source_start_seconds"],
                "source_end_seconds": when,
                "right_censored": False,
                "partial_observation": False,
                "measurement_complete": True,
                "scalar_scope": "qualified_snap_to_first_movement",
                "trajectory": [_slim(p) for p in path if p["timestamp_seconds"] <= when],
                "series": [
                    {
                        "timestamp_seconds": when,
                        "event_id": movement["id"],
                        "state": "first_movement",
                        "shot_id": _shot(movement),
                        "visibility_state": "observed",
                    }
                ],
                "event_evidence": evidence([participation, snaps[0], movement], timeline),
                "recognition_inferred": False,
            },
            actor["id"],
        )
    ]


def _temporal_packet(actor, participation, start, end, timeline):
    attrs = participation.get("attributes") or {}
    return {
        "actor_id": actor["id"],
        "actor_role": actor.get("observed_role"),
        "team_label": actor.get("team_label"),
        "role_evidence": actor.get("role_evidence") or [],
        "track_id": actor["id"],
        "opponent_id": None,
        "subject_id": participation["id"],
        "participation_event_id": participation["id"],
        "opportunity_kind": attrs["opportunity_kind"],
        "source_start_seconds": start,
        "source_end_seconds": end,
        "shot_id": _shot(participation),
        "trajectory": [],
        "attacking_direction": attrs.get("attacking_direction"),
        "series_interpolation": "none",
        "event_evidence": evidence([participation], timeline),
        "required_evidence": [
            "qualified_position_participation",
            "resolved_role",
            "explicit_team",
            "native_source_timestamps",
            "same_known_shot",
        ],
    }


def _special_teams(actor, participation, packet, events, by_id, timeline, *, landing_safe):
    eligible = [e for e in events if _event_matches(e, actor, packet, timeline)]
    rows = []
    kind = packet["opportunity_kind"]
    for kick in eligible:
        attrs = kick.get("attributes") or {}
        operation = attrs.get("operation_id")
        expected = "punt" if kind == "punts" else "kick"
        if kick.get("event_type") != expected or not operation:
            continue
        linked = [
            e for e in eligible if (e.get("attributes") or {}).get("operation_id") == operation
        ]
        # Ambiguous duplicate kick/snap/landing observations are unavailable.
        if sum(e.get("event_type") == expected for e in linked) != 1:
            continue
        snaps = [e for e in linked if e.get("event_type") == "snap"]
        kick_time = event_time(kick, timeline)
        if len(snaps) == 1 and event_time(snaps[0], timeline) <= kick_time:
            start = event_time(snaps[0], timeline)
            operation_path = _path(actor, start, kick_time, _shot(kick), field=False)
            if operation_path and operation_path[-1]["timestamp_seconds"] >= kick_time - 1e-8:
                rows.append(
                    row(
                        "operation_time",
                        {
                            **packet,
                            "operation_id": operation,
                            "seconds": kick_time - start,
                            "source_start_seconds": start,
                            "source_end_seconds": kick_time,
                            "right_censored": False,
                            "actor_observations": [
                                {
                                    key: p.get(key)
                                    for key in (
                                        "timestamp_seconds",
                                        "frame_number",
                                        "bbox",
                                        "shot_id",
                                        "visibility_state",
                                    )
                                }
                                for p in operation_path
                            ],
                            "series": evidence([snaps[0], kick], timeline),
                            "event_evidence": evidence([participation, snaps[0], kick], timeline),
                        },
                        actor["id"],
                    )
                )
        landings = [
            e
            for e in linked
            if e.get("event_type") == "landing" and event_time(e, timeline) > kick_time
        ]
        if len(landings) != 1:
            continue
        landing = landings[0]
        end = event_time(landing, timeline)
        ball_id = attrs.get("ball_id")
        if not ball_id or (landing.get("attributes") or {}).get("ball_id") != ball_id:
            continue
        ball = by_id.get(str(ball_id))
        if not ball or not _role(ball, {"BALL"}, "special_teams"):
            continue
        ball_path = _path(ball, kick_time, end, _shot(kick), field=False)
        if not ball_path or ball_path[-1]["timestamp_seconds"] < end - 1e-8:
            continue
        base = {
            **packet,
            "operation_id": operation,
            "ball_id": ball_id,
            "source_start_seconds": kick_time,
            "source_end_seconds": end,
            "right_censored": False,
            "series": evidence([kick, landing], timeline),
            # An airborne image location is not a calibrated ground position.
            # Preserve visibility evidence without projecting an aerial path
            # onto the field. Landing geometry comes from its qualified event.
            "trajectory": [],
            "ball_observations": [
                {
                    key: p.get(key)
                    for key in (
                        "timestamp_seconds",
                        "frame_number",
                        "bbox",
                        "shot_id",
                        "visibility_state",
                    )
                }
                for p in ball_path
            ],
            "event_evidence": evidence([participation, kick, landing], timeline),
        }
        rows.append(row("hang_time", {**base, "seconds": end - kick_time}, actor["id"]))
        location = landing.get("attributes") or {}
        point = {
            "field_x": number(location.get("field_x")),
            "field_y": number(location.get("field_y")),
            "visibility_state": "observed",
        }
        if landing_safe and visible(point, field=True):
            rows.append(
                row(
                    "kick_landing_location",
                    {
                        **base,
                        **point,
                        "timestamp_seconds": end,
                        "landing_event_id": landing["id"],
                        "series": [
                            {
                                **_slim(point),
                                "timestamp_seconds": end,
                                "shot_id": _shot(landing),
                                "event_id": landing["id"],
                            }
                        ],
                    },
                    actor["id"],
                    "field_location",
                )
            )
    return rows


def _te_transitions(actor, participation, packet, events, by_id, timeline):
    observations = [
        e
        for e in events
        if e.get("event_type") == "te_usage" and _event_matches(e, actor, packet, timeline)
    ]
    if len(observations) < 2:
        return []
    observations.sort(key=lambda e: event_time(e, timeline))
    interaction = (observations[0].get("attributes") or {}).get("interaction_id")
    states = [(e.get("attributes") or {}).get("state") for e in observations]
    # A transition must explicitly enter route participation from block/chip.
    if (
        not interaction
        or tuple(states) not in {("block", "route"), ("chip", "route"), ("block", "chip", "route")}
        or any(
            (e.get("attributes") or {}).get("interaction_id") != interaction for e in observations
        )
        or len({event_time(e, timeline) for e in observations}) != len(observations)
    ):
        return []
    opponent_ids = {
        (e.get("attributes") or {}).get("opponent_id")
        for e, state in zip(observations, states)
        if state != "route"
    }
    if len(opponent_ids) != 1:
        return []
    for observation, state in zip(observations, states):
        if state == "route":
            continue
        opponent = by_id.get(str((observation.get("attributes") or {}).get("opponent_id")))
        if not opponent or not _role(opponent, DEFENSE_ROLES, "defense"):
            return []
        pts = _path(
            opponent,
            packet["source_start_seconds"],
            event_time(observations[-1], timeline),
            _shot(participation),
        )
        if not pts or pts[-1]["timestamp_seconds"] < event_time(observations[-1], timeline) - 1e-8:
            return []
    start, end = event_time(observations[0], timeline), event_time(observations[-1], timeline)
    return [
        row(
            "te_usage_transition",
            {
                **packet,
                "interaction_id": interaction,
                "opponent_id": next(iter(opponent_ids)),
                "seconds": end - start,
                "source_start_seconds": start,
                "source_end_seconds": end,
                "right_censored": False,
                "partial_observation": False,
                "measurement_complete": True,
                "scalar_scope": "qualified_block_or_chip_to_route",
                "trajectory": [
                    p for p in packet["trajectory"] if start <= p["timestamp_seconds"] <= end
                ],
                "series": [
                    {
                        **evidence([e], timeline)[0],
                        "state": state,
                        "actor_id": actor["id"],
                        "opponent_id": (e.get("attributes") or {}).get("opponent_id"),
                        "visibility_state": "observed",
                    }
                    for e, state in zip(observations, states)
                ],
                "event_evidence": evidence([participation, *observations], timeline),
            },
            actor["id"],
        )
    ]
