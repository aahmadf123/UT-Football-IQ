"""Source-time event sequences and conservative within-recording associations.

Camera shots, event sequences and canonical actions are separate records. Pixel
and OCR heuristics produce review proposals, never qualified snap counts. The
same interface accepts explicit human/qualified event evidence without requiring
scoreboard cards or a particular camera order.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

ASSOCIATION_VERSION = "film-events-2026-10-21.1"
_STARTS = {"snap", "motion_onset"}
_ENDS = {"dead_ball", "terminal", "whistle", "motion_settled"}
_NONPLAYS = {"false_start", "aborted_snap", "no_play"}


def _finite(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _trusted(evidence: dict[str, Any], _film_type: str) -> bool:
    # No automatic event/replay model has passed the required frozen-evidence
    # qualification contract. A worker-supplied flag/model ID is not approval.
    return evidence.get("source") in {"human", "manual"}


def _key(*values: Any) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:20]


def _situation(shot: dict[str, Any]) -> tuple[Any, ...] | None:
    board = shot.get("scoreboard") or {}
    if not all(_finite(board.get(k)) for k in ("quarter", "clock_seconds", "down")):
        return None
    return tuple(board.get(k) for k in ("quarter", "clock_seconds", "down", "distance", "ball_on", "possession"))


def _event_pattern(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("action_onset_seconds") is None or b.get("action_onset_seconds") is None:
        return False
    if a.get("terminal_seconds") is None or b.get("terminal_seconds") is None:
        return False
    first = a["terminal_seconds"] - a["action_onset_seconds"]
    second = b["terminal_seconds"] - b["action_onset_seconds"]
    return first > 0 and second > 0 and abs(first - second) <= max(.4, .15 * max(first, second))


def _sequence(shot: dict[str, Any], start: float, end: float, *, kind: str,
              onset: dict[str, Any] | None = None, terminal: dict[str, Any] | None = None,
              film_type: str = "unknown") -> dict[str, Any]:
    events = shot.get("events") or []
    observed_snap = onset if onset and onset["event_type"] == "snap" else None
    snap = observed_snap["timestamp_seconds"] if observed_snap else None
    sequence_id = _key(shot["id"], start, end, kind, snap)
    penalties = [event for event in events if event["event_type"] == "penalty"
                 and start <= event["timestamp_seconds"] <= end]
    complete = bool(terminal and terminal["event_type"] != "motion_settled" and _trusted(terminal, film_type))
    qualified = bool(observed_snap and terminal and _trusted(observed_snap, film_type)
                     and _trusted(terminal, film_type) and terminal["event_type"] != "motion_settled"
                     and kind == "live_action" and all(e.get("counts_as_play") is True for e in penalties))
    return {"id": sequence_id, "shot_id": shot["id"], "start_seconds": start, "end_seconds": end,
            "snap_seconds": snap, "snap_candidate_seconds": onset["timestamp_seconds"] if onset else None,
            "snap_confidence": observed_snap.get("confidence", 0.) if observed_snap else None,
            "action_onset_seconds": onset["timestamp_seconds"] if onset else None,
            "terminal_seconds": terminal["timestamp_seconds"] if terminal else None,
            "sequence_kind": kind, "live_action_complete": complete,
            "canonical_id": f"action-{sequence_id}", "canonical_count_eligible": qualified,
            "canonical_view_eligible": qualified,
            "association_status": "independent" if qualified else "unresolved",
            "view": shot.get("view", "unknown"), "view_confidence": shot.get("view_confidence", 0.),
            "confidence": min([float(e.get("confidence", 0.)) for e in (onset, terminal) if e] or [0.]),
            "scene_kind": shot.get("scene_kind", "unknown"), "scoreboard": shot.get("scoreboard") or {},
            "evidence": copy.deepcopy([event for event in events if start <= event["timestamp_seconds"] <= end]),
            "source_clock": "nominal_fps_fallback" if shot.get("timestamp_verified") is False else "presentation_seconds",
            "result_version": ASSOCIATION_VERSION,
            "review_required": not qualified,
            "exclusion_reason": None if qualified else (
                "non_countable_sequence" if kind in _NONPLAYS else
                "incomplete_live_action" if onset and not complete else "unqualified_event_evidence")}


def associate_shots(shots: list[dict[str, Any]], *, duration: float,
                    film_type: str = "unknown") -> dict[str, Any]:
    """Build reviewable actions/context and propose links without dropping film.

    Inputs contain actual source-time observations. Repeated board text alone
    cannot associate plays. Automatic angle links require both situation and
    event-pattern agreement; replay links require explicit/repeated-image evidence.
    Every association retains its evidence and approval state.
    """
    if not _finite(duration) or duration <= 0:
        raise ValueError("Source duration must be finite and positive")
    ordered = copy.deepcopy(shots)
    ordered.sort(key=lambda shot: shot["start_seconds"])
    rows: list[dict[str, Any]] = []
    associations: list[dict[str, Any]] = []
    rejected = 0
    previous_shot_end = 0.
    by_shot: dict[str, dict[str, Any]] = {}
    for shot in ordered:
        start, end = shot.get("start_seconds"), shot.get("end_seconds")
        if not _finite(start) or not _finite(end) or not 0 <= start < end <= duration + 1e-6:
            raise ValueError("Camera shot interval must lie inside source duration")
        if start < previous_shot_end - 1e-6:
            raise ValueError("Camera shot intervals cannot overlap")
        previous_shot_end = end
        if not shot.get("id") or shot["id"] in by_shot:
            raise ValueError("Camera shots need unique IDs")
        by_shot[shot["id"]] = shot
        valid = []
        for event in shot.get("events", []):
            stamp = event.get("timestamp_seconds")
            confidence = event.get("confidence", 0.)
            if (not _finite(stamp) or not start <= stamp < end or not isinstance(event.get("event_type"), str)
                    or not _finite(confidence) or not 0 <= confidence <= 1):
                rejected += 1
            else:
                valid.append(event)
        shot["events"] = sorted(valid, key=lambda event: event["timestamp_seconds"])
        onsets = [event for event in shot["events"] if event["event_type"] in _STARTS]
        # A corroborated snap supersedes a motion-onset candidate in its local
        # detection window; it must not create two actions from the same burst.
        onsets = [event for event in onsets if event["event_type"] == "snap" or not any(
            other["event_type"] == "snap" and abs(other["timestamp_seconds"] - event["timestamp_seconds"]) <= 1.
            for other in onsets)]
        made = []
        for index, onset in enumerate(onsets):
            following = onsets[index + 1]["timestamp_seconds"] if index + 1 < len(onsets) else end
            terminal = next((event for event in shot["events"] if event["event_type"] in _ENDS
                             and onset["timestamp_seconds"] < event["timestamp_seconds"] < following), None)
            a = max(start, onset["timestamp_seconds"] - 3.)
            b = min(following, end, terminal["timestamp_seconds"] + 2. if terminal else end)
            row = _sequence(shot, a, b, kind="live_action" if onset["event_type"] == "snap" else "action_candidate",
                            onset=onset, terminal=terminal, film_type=film_type)
            made.append(row)
        for event in shot["events"]:
            if event["event_type"] in _NONPLAYS and _trusted(event, film_type):
                stamp = event["timestamp_seconds"]
                following = min([e["timestamp_seconds"] for e in onsets if e["timestamp_seconds"] > stamp] or [end])
                affected = [row for row in made if row.get("action_onset_seconds") is not None
                            and row["action_onset_seconds"] <= stamp
                            <= (row["terminal_seconds"] if row["terminal_seconds"] is not None else following)]
                for row in affected:
                    row["canonical_count_eligible"] = False
                    row["canonical_view_eligible"] = False
                    row["exclusion_reason"] = "non_countable_sequence"
                a = min([row["start_seconds"] for row in affected] or [max(start, stamp-3)])
                b = max([row["end_seconds"] for row in affected] or [min(end, following, stamp+2)])
                made.append(_sequence(shot, a, b, kind=event["event_type"], film_type=film_type))
        if not made and shot.get("replay_evidence"):
            made.append(_sequence(shot, start, end, kind="replay_candidate", film_type=film_type))
        if not made:
            kind = "context" if shot.get("scene_kind") in {"card", "scoreboard", "crowd", "graphic", "commercial"} else "unresolved"
            made.append(_sequence(shot, start, end, kind=kind, film_type=film_type))
        for row in sorted(made, key=lambda row: row["start_seconds"]):
            previous = [r for r in rows if r["sequence_kind"] not in {"context", "unresolved", "replay", "replay_candidate"}]
            replay = shot.get("replay_evidence")
            explicit = shot.get("association_evidence")
            target, kind, evidence, status = None, None, None, "proposed"
            if replay or explicit:
                evidence = replay or explicit
                target_id = evidence.get("target_sequence_id")
                target_shot = evidence.get("target_shot_id")
                candidates = [r for r in previous if (target_id and r["id"] == target_id)
                              or (target_shot and r["shot_id"] == target_shot)]
                if len(candidates) == 1:
                    target = candidates[0]
                kind = "replay" if replay else explicit.get("kind", "alternate_angle")
                status = "confirmed" if _trusted(evidence, film_type) else "proposed"
                if status == "confirmed" and explicit and kind == "alternate_angle" and target is None:
                    # Staff established this is an additional view, but a shot
                    # containing multiple actions is not a unique canonical key.
                    row["canonical_count_eligible"] = False
                    row["canonical_view_eligible"] = False
                    row["association_status"] = "unresolved"
                    row["review_required"] = True
                    row["exclusion_reason"] = "ambiguous_canonical_target"
                if replay:
                    row["sequence_kind_candidate"] = "replay"
                    if status == "confirmed" or not row["canonical_count_eligible"]:
                        row["sequence_kind"] = "replay" if status == "confirmed" else "replay_candidate"
                        row["canonical_count_eligible"] = False
                        row["canonical_view_eligible"] = False
                        row["association_status"] = "unresolved"
                        row["exclusion_reason"] = "replay"
            elif row["sequence_kind"] not in {"context", "unresolved", *_NONPLAYS}:
                situation = _situation(shot)
                candidates = [r for r in previous if r["shot_id"] != shot["id"]
                              and row["start_seconds"] - r["end_seconds"] <= 120
                              and situation is not None and _situation(by_shot[r["shot_id"]]) == situation
                              and r["view"] in {"sideline", "endzone"} and row["view"] in {"sideline", "endzone"}
                              and r["view"] != row["view"] and _event_pattern(r, row)]
                if len(candidates) == 1:
                    target, kind = candidates[0], "alternate_angle"
                    evidence = {"source": "situation_and_event_pattern", "validated": False,
                                "situation": list(situation), "matched_event_duration": True}
            if target is not None:
                if status == "confirmed":
                    row["canonical_view_eligible"] = bool(kind == "alternate_angle"
                        and row["canonical_view_eligible"] and target["canonical_view_eligible"])
                    row["canonical_id"] = target["canonical_id"]
                    row["canonical_count_eligible"] = False
                    row["association_status"] = status
                    row["review_required"] = False
                    row["exclusion_reason"] = "replay" if kind == "replay" else "additional_view"
                else:
                    row["association_candidate"] = {"canonical_id": target["canonical_id"],
                                                     "target_sequence_id": target["id"], "kind": kind}
                    row["review_required"] = True
                associations.append({"from_sequence_id": row["id"], "to_sequence_id": target["id"],
                                     "canonical_id": target["canonical_id"], "kind": kind, "status": status,
                                     "evidence": evidence, "result_version": ASSOCIATION_VERSION})
            rows.append(row)
        # Retain all setup/tail/context pixels outside the padded action windows.
        edges = sorted((max(start, r["start_seconds"]), min(end, r["end_seconds"])) for r in made)
        cursor = start
        for a, b in edges:
            if a > cursor:
                rows.append(_sequence(shot, cursor, a, kind="context", film_type=film_type))
            cursor = max(cursor, b)
        if cursor < end:
            rows.append(_sequence(shot, cursor, end, kind="context", film_type=film_type))
    # A corrupt/incomplete shot detector must not erase an uncovered source span.
    coverage = sorted((r["start_seconds"], r["end_seconds"]) for r in rows)
    cursor = 0.
    for a, b in coverage:
        if a > cursor:
            missing = {"id": f"unassigned-{cursor}", "events": [], "scene_kind": "unknown"}
            rows.append(_sequence(missing, cursor, a, kind="unresolved"))
        cursor = max(cursor, b)
    if cursor < duration:
        rows.append(_sequence({"id": "unassigned-tail", "events": []}, cursor, duration, kind="unresolved"))
    rows.sort(key=lambda row: (row["start_seconds"], row["end_seconds"], row["id"]))
    covered, end_cursor, gaps = 0., 0., []
    for row in rows:
        a, b = row["start_seconds"], min(duration, row["end_seconds"])
        if a > end_cursor:
            gaps.append([end_cursor, a])
        covered += max(0., b - max(a, end_cursor))
        end_cursor = max(end_cursor, b)
    canonical_numbers = {key: index + 1 for index, key in enumerate(dict.fromkeys(row["canonical_id"] for row in rows))}
    specs = [{"start_time": row["start_seconds"], "end_time": row["end_seconds"],
              "play_number": canonical_numbers[row["canonical_id"]], "view": row["view"],
              "view_confidence": row["view_confidence"], "canonical_count_eligible": row["canonical_count_eligible"],
              "logical_play_status": "observed" if row["canonical_count_eligible"] else row["sequence_kind"],
              "scoreboard": row["scoreboard"], "sequence": row} for row in rows]
    return {"schema_version": 1, "result_version": ASSOCIATION_VERSION, "film_type": film_type,
            "sequences": rows, "associations": associations, "clip_specs": specs,
            "coverage": {"source_seconds": duration, "covered_seconds": covered, "gaps": gaps,
                         "meaning": "source_interval_retention_not_live_action_accuracy"},
            "diagnostics": {"rejected_events": rejected, "shot_count": len(ordered),
                            "qualified_canonical_actions": sum(row["canonical_count_eligible"] for row in rows)}}
