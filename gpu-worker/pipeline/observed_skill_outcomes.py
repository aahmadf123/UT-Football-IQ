"""Reviewed RB/receiver/defender outcomes; motion never assigns football results.

Staff supply possession IDs and actual actor/pair references. These observations
remain experimental until their film population and producer are approved.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pipeline.observed_position_products import _path
from pipeline.position_evidence import event_shot, event_time, evidence, qualified, row

EVENT_TYPES = {
    "pass_target",
    "pass_result",
    "reception",
    "possession_end",
    "carrier_cut",
    "tackle_outcome",
}


def _real(packet):
    for layer in (packet, packet.get("provenance") or {}, packet.get("attributes") or {}):
        if any(
            token in str(layer.get("source", "")).lower()
            for token in ("synthetic", "stub", "mock", "test", "placeholder")
        ):
            return False
    return True


def _actor(by_id, identity, side=None):
    track = by_id.get(str(identity))
    if track is None or not qualified(track) or not _real(track):
        return None
    if side:
        declarations = {
            track[key]
            for key in ("team_label", "side_of_ball")
            if track.get(key) in {"offense", "defense", "special_teams"}
        }
        if declarations != {side}:
            return None
    return track


def _packet(used, timeline, possession, **values):
    return {
        "source": "real",
        "measurement": "reviewed_event_outcome",
        "possession_id": possession,
        "event_evidence": evidence(used, timeline),
        "eligible_population": "explicit_reviewed_possession_events",
        **values,
    }


def _completed_reception(catch, results, timeline):
    attrs = catch["attributes"]
    return (
        attrs.get("forward_pass") is True
        and attrs.get("completion_observed") is True
        and attrs.get("possession_side") == "offense"
        and (
            not results
            or (
                len(results) == 1
                and results[0]["attributes"].get("outcome") == "complete"
                and results[0]["attributes"].get("actor_id") == attrs["actor_id"]
                and event_shot(results[0]) == event_shot(catch)
                and event_time(results[0], timeline) >= event_time(catch, timeline)
            )
        )
    )


def observed_skill_outcomes(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    timeline=None,
    analytics_safe=False,
    attacking_direction=None,
):
    if timeline is not None and timeline.verified is not True:
        return []
    by_id = {str(track["id"]): track for track in tracklets if track.get("id") is not None}
    if len(by_id) != len(tracklets):
        return []
    groups = defaultdict(list)
    seen, conflicts = {}, set()
    by_event_id, conflicting_ids = {}, set()
    for event in events:
        event_id = event.get("id")
        if event_id in by_event_id and by_event_id[event_id] != event:
            conflicting_ids.add(event_id)
        else:
            by_event_id[event_id] = event
        attrs = event.get("attributes") or {}
        possession = attrs.get("possession_id")
        if (
            event.get("event_type") not in EVENT_TYPES
            or not event.get("id")
            or not qualified(event)
            or not _real(event)
            or event_time(event, timeline) is None
            or not event_shot(event)
            or not isinstance(possession, str)
            or not possession.strip()
            or _actor(by_id, attrs.get("actor_id")) is None
        ):
            continue
        key = (
            event["event_type"],
            possession,
            str(attrs["actor_id"]),
            attrs.get("opponent_id"),
            event_time(event, timeline),
        )
        if key in seen and seen[key] != event:
            # Duplicate staff annotations are not additional outcomes. Conflicts need review.
            if seen[key].get("attributes") != attrs:
                conflicts.add(key)
        else:
            seen[key] = event
    for key, event in seen.items():
        if event.get("id") in conflicting_ids:
            conflicts.add(key)
        if key not in conflicts:
            groups[key[1]].append(event)
    conflicted_possessions = {
        key[1] for key in conflicts if key[0] in {"pass_result", "reception", "possession_end"}
    }
    rows = []
    for possession, observations in groups.items():
        selected = defaultdict(list)
        for event in observations:
            selected[event["event_type"]].append(event)
        targets, results = selected["pass_target"], selected["pass_result"]
        receptions, endings = selected["reception"], selected["possession_end"]
        if len(targets) == 1:
            target, attrs = targets[0], targets[0]["attributes"]
            receiver = _actor(by_id, attrs["actor_id"], "offense")
            if receiver is not None and attrs.get("forward_pass") is True:
                receiver_id = str(receiver["id"])
                rows.append(
                    row(
                        "observed_target",
                        _packet([target], timeline, possession, count=1),
                        receiver_id,
                        "rep",
                    )
                )
                if len(results) == 1:
                    result, outcome = results[0], results[0]["attributes"]
                    state = outcome.get("outcome")
                    reception_consistent = not receptions or (
                        len(receptions) == 1
                        and receptions[0]["attributes"].get("actor_id") == receiver_id
                        and _completed_reception(receptions[0], results, timeline)
                    )
                    if (
                        reception_consistent
                        and possession not in conflicted_possessions
                        and outcome.get("actor_id") == receiver_id
                        and state in {"complete", "incomplete", "interception"}
                        and event_shot(result) == event_shot(target)
                        and event_time(result, timeline) >= event_time(target, timeline)
                    ):
                        packet = _packet(
                            [target, result],
                            timeline,
                            possession,
                            completed=int(state == "complete"),
                            outcome=state,
                            target_event_id=target["id"],
                            outcome_event_id=result["id"],
                        )
                        rows.append(row("target_catch_outcome", packet, receiver_id, "boolean"))
                        if outcome.get("contested") is True:
                            rows.append(
                                row(
                                    "contested_target_outcome",
                                    {**packet, "contested": True},
                                    receiver_id,
                                    "boolean",
                                )
                            )
                        defender = _actor(by_id, attrs.get("opponent_id"), "defense")
                        if (
                            attrs.get("responsibility_confirmed") is True
                            and defender is not None
                            and str(defender["id"]) != receiver_id
                        ):
                            rows.append(
                                row(
                                    "coverage_target_outcome",
                                    {
                                        **packet,
                                        "receiver_id": receiver_id,
                                        "responsibility_confirmed": True,
                                    },
                                    str(defender["id"]),
                                    "boolean",
                                )
                            )
        if (
            analytics_safe
            and type(attacking_direction) is int
            and attacking_direction in (-1, 1)
            and len(receptions) == len(endings) == 1
        ):
            catch, end = receptions[0], endings[0]
            attrs, terminal = catch["attributes"], end["attributes"]
            actor = _actor(by_id, attrs["actor_id"], "offense")
            start, finish, shot = (
                event_time(catch, timeline),
                event_time(end, timeline),
                event_shot(catch),
            )
            consistent_result = possession not in conflicted_possessions and _completed_reception(
                catch, results, timeline
            )
            if (
                consistent_result
                and actor is not None
                and attrs.get("forward_pass") is True
                and attrs.get("completion_observed") is True
                and attrs.get("possession_side") == "offense"
                and terminal.get("actor_id") == attrs["actor_id"]
                and terminal.get("possession_ended") is True
                and terminal.get("terminal_reason")
                in {"tackle", "out_of_bounds", "touchdown", "fumble", "dead_ball"}
                and finish > start
                and event_shot(end) == shot
            ):
                points = _path(actor, start, finish, shot)
                if (
                    points
                    and abs(points[-1]["timestamp_seconds"] - finish) < 1e-8
                    and all(_real(point) for point in points)
                ):
                    rows.append(
                        row(
                            "observed_yac",
                            _packet(
                                [catch, end],
                                timeline,
                                possession,
                                yards=attacking_direction
                                * (points[-1]["field_x"] - points[0]["field_x"]),
                                attacking_direction=attacking_direction,
                                source_start_seconds=start,
                                source_end_seconds=finish,
                                trajectory=points,
                                end_reason=terminal["terminal_reason"],
                            ),
                            str(actor["id"]),
                            "yd",
                        )
                    )
        for event in selected["carrier_cut"]:
            attrs = event["attributes"]
            actor = _actor(by_id, attrs["actor_id"], "offense")
            when, shot = event_time(event, timeline), event_shot(event)
            if (
                not analytics_safe
                or attrs.get("cut_observed") is not True
                or actor is None
                or possession in conflicted_possessions
            ):
                continue
            starts = [
                e
                for e in observations
                if e["event_type"] == "reception"
                and e["attributes"].get("actor_id") == attrs["actor_id"]
                and event_time(e, timeline) <= when
                and event_shot(e) == shot
                and _completed_reception(e, results, timeline)
            ]
            starts.extend(
                e
                for e in events
                if e.get("event_type") in {"handoff", "possession_start"}
                and qualified(e)
                and _real(e)
                and (e.get("attributes") or {}).get("possession_id") == possession
                and (e.get("attributes") or {}).get("actor_id") == attrs["actor_id"]
                and event_time(e, timeline) is not None
                and event_time(e, timeline) <= when
                and event_shot(e) == shot
            )
            point = _path(actor, when, when, shot)
            ended = any(
                e["attributes"].get("actor_id") == attrs["actor_id"]
                and e["attributes"].get("possession_ended") is True
                and event_time(e, timeline) <= when
                for e in endings
            )
            if not ended and len(starts) == 1 and point and _real(point[0]):
                rows.append(
                    row(
                        "observed_cut_location",
                        _packet(
                            [starts[0], event],
                            timeline,
                            possession,
                            field_x=point[0]["field_x"],
                            field_y=point[0]["field_y"],
                            timestamp_seconds=when,
                            actor_id=str(actor["id"]),
                            location_event_id=event["id"],
                            shot_id=shot,
                            visibility_state="observed",
                            cut_event_id=event["id"],
                        ),
                        str(actor["id"]),
                        "field_location",
                    )
                )
        for event in selected["tackle_outcome"]:
            attrs = event["attributes"]
            defender = _actor(by_id, attrs["actor_id"], "defense")
            carrier = _actor(by_id, attrs.get("opponent_id"), "offense")
            state = attrs.get("outcome")
            if (
                defender is None
                or carrier is None
                or str(defender["id"]) == str(carrier["id"])
                or attrs.get("tackle_observed") is not True
                or state not in {"made", "missed", "assist"}
            ):
                continue
            packet = _packet(
                [event],
                timeline,
                possession,
                count=1,
                outcome=state,
                defender_id=str(defender["id"]),
                carrier_id=str(carrier["id"]),
            )
            name = {
                "made": "observed_tackle",
                "missed": "observed_missed_tackle",
                "assist": "observed_tackle_assist",
            }[state]
            rows.append(row(name, packet, str(defender["id"]), "count"))
            if state == "missed" and attrs.get("forced_by_carrier") is True:
                rows.append(
                    row(
                        "observed_forced_missed_tackle",
                        {**packet, "forced_by_carrier": True},
                        str(carrier["id"]),
                        "count",
                    )
                )
    return rows
