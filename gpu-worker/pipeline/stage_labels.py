"""Stage 8 — Football Labels (Phase 2 enhanced).

Produces formation/coverage/route labels for each clip using rule-based
heuristics on the tracking data.

Phase 1 labels:
  - offensive_formation — shotgun / under_center / pistol / trips / empty
  - defensive_front     — 4-3 / 3-4 / 5-2 / nickel / dime / quarters
  - motion_detected     — boolean
  - blitz_candidate     — boolean
  - field_orientation   — which way the offence attacks in template x (±1),
                          or unresolved

``play_direction`` used to be emitted here on every play. It was two different
things wearing one name: downstream football code (self-scout, tendency breaks)
reads the *presence* of that label as "this was a run, and it went this way",
while the orchestrator read its *value* as the field-frame attacking direction.
Emitting it unconditionally therefore made every play a run. The orientation it
was really computing is now ``field_orientation``, and the run-direction label
is left to whatever can actually tell a run from a pass.

Phase 2 additions:
  - shift_detected      — pre-snap shift (distinct from motion)
  - motion_type         — jet / orbit / fly / trade / return
  - personnel_grouping  — 11 / 12 / 21 / 22 / 13 / 10 / 00 (RB count + TE count)
  - formation_strength  — boundary / field / balanced
  - formation_family    — expanded: spread / pro / i_form / singleback / jumbo / ...

All labels are tagged source="model" with a confidence value.
Manual corrections go through the Corrections API and are later exported as Labels.

Output: `labels` rows written to the backend.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog

from pipeline import backend, object_store
from pipeline.detection.official_suppressor import filter_non_players
from pipeline.homography import field_orientation as fo
from pipeline.runtime import PipelineInterrupted, check_control
from pipeline.team_classification import (
    ClassificationResult,
    KMeansTeamClassifier,
    classify_tracklet_frames,
)
from pipeline.team_classification.sides import (
    TEAM_DEFENSE,
    TEAM_OFFENSE,
    SideContext,
    SideDecision,
    resolve_offense_cluster,
)

log = structlog.get_logger(__name__)

#: Frames read for jersey colours. They are spread so every track gets
#: looks across its own life: thirty consecutive frames from the snap (half a
#: second) left most post-snap fragments without a single sample, and 59% of
#: a game's tracks unlabelled.
TEAM_CLASSIFICATION_MAX_FRAMES = 90
#: Looks per track, spread over its life, before frames are capped.
TEAM_SAMPLES_PER_TRACK = 4

# Frames voted over when resolving which way the offence attacks. Per frame the
# estimator sorts the players twice, so the cost is small and the cap is about
# the vote rather than the arithmetic: 60 evenly spaced frames span the whole
# clip, and agreement over that many has long since stopped moving.
ORIENTATION_MAX_FRAMES = 60


def run(
    clip_id: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    fps: float,
    input_uri: str | None = None,
    frames_by_number: dict[int, Any] | None = None,
    side_context: SideContext | None = None,
) -> dict[str, Any]:
    """Generate formation and coverage labels for a clip.

    ``side_context`` is what the official play-by-play says about the play
    (Toledo's side of the ball, and whether Toledo wears the lighter kit);
    with it, team colours become offense and defense even when the formation
    is not visible on a calibrated field.
    """
    log.info("stage_labels_start", clip_id=clip_id)
    synthetic = any(
        (track.get("provenance") or {}).get("source") == "synthetic_test"
        or any(point.get("source") == "synthetic_test" for point in track.get("track_points", []))
        for track in tracklets
    ) or any(
        event.get("source") == "synthetic_test"
        or (event.get("provenance") or {}).get("source") == "synthetic_test"
        or ((event.get("attributes") or {}).get("provenance") or {}).get("source") == "synthetic_test"
        for event in events
    )

    # ── Official / sideline suppression (Issue #148) ──────────────────────
    # Drop officials and off-field figures before any team/formation/personnel
    # analysis so referees never corrupt formation or personnel counts.
    # Backward compatible: tracklets without a class/role field are kept.
    total_tracklets = len(tracklets)
    tracklets = filter_non_players(tracklets)
    suppressed = total_tracklets - len(tracklets)
    if suppressed:
        log.info(
            "stage_labels_officials_suppressed",
            clip_id=clip_id,
            suppressed=suppressed,
            remaining=len(tracklets),
        )

    snap_event = next((e for e in events if e.get("event_type") == "snap"), None)
    snap_frame = snap_event.get("frame_number", 0) if snap_event else 0

    classifier = KMeansTeamClassifier()
    team_results = _classify_team_colors(
        tracklets,
        snap_frame=snap_frame,
        input_uri=input_uri,
        frames_by_number=frames_by_number,
        classifier=classifier,
    )
    staff_players = {
        str(track.get("id")): track for track in tracklets
        if (track.get("provenance") or {}).get("actor_kind") == "player"
        and ((track.get("provenance") or {}).get("actor_classification") or {}).get("method") == "staff_review"
    }
    # A stripe/color guess cannot turn a staff-corrected player back into an
    # official. With no observed unit, retain unknown rather than guess a side.
    team_results = [replace(result, label="unknown", confidence=0.0, cluster_index=None,
                            reason="staff_player_kind_overrides_stripes")
                    if result.tracklet_id in staff_players and result.label == "official"
                    else result for result in team_results]
    team_results = [replace(result, label=staff_players[result.tracklet_id]["team_label"],
                            confidence=1.0, cluster_index=None, reason="staff_observed_unit")
                    if result.tracklet_id in staff_players
                    and (staff_players[result.tracklet_id].get("provenance") or {}).get("unit_source") == "staff_review"
                    and staff_players[result.tracklet_id].get("team_label") in {"home", "away", "offense", "defense", "special_teams", "unknown"}
                    else result for result in team_results]
    team_results, sides = _name_team_sides(
        team_results, tracklets, classifier, side_context or SideContext()
    )
    team_by_tracklet = _aggregate_team_results(team_results)
    # DB patches alone do not update the observation bundle used by subsequent
    # stages in this process. Return enriched records as well.
    tracklets = [
        {**t, "team_label": team_by_tracklet[str(t.get("id"))].label,
         "team_confidence": team_by_tracklet[str(t.get("id"))].confidence}
        if str(t.get("id")) in team_by_tracklet else dict(t)
        for t in tracklets
    ]
    official_tracklet_ids = {
        tracklet_id
        for tracklet_id, result in team_by_tracklet.items()
        if result.label == "official"
    }
    analysis_tracklets = [
        t
        for t in tracklets
        if str(t.get("id") or t.get("tracklet_id") or "") not in official_tracklet_ids
    ]

    # Build snap-frame centroid list
    snap_positions = _positions_at_frame(analysis_tracklets, snap_frame)

    labels: list[dict[str, Any]] = []
    if snap_event is None:
        ids = _write_team_labels(clip_id, team_by_tracklet, synthetic=synthetic)
        return {
            "label_count": len(ids), "label_ids": ids, "labels": [], "tracklets": tracklets,
            "skipped": True, "reason": "snap_not_observed",
            "team_classification": {"classified_tracklets": len(team_by_tracklet),
                                    "fallback": "no_visual_samples" if not team_by_tracklet else None,
                                    "sides": sides.evidence},
        }

    # ── Offensive formation (enhanced) ────────────────────────────────────
    off_formation, off_conf = _classify_offensive_formation(snap_positions)
    labels.append(
        {
            "label_type": "offensive_formation",
            "label_value": {"formation": off_formation, "confidence": off_conf},
        }
    )

    # ── Formation family (Phase 2) ────────────────────────────────────────
    family, family_conf = _classify_formation_family(snap_positions)
    labels.append(
        {
            "label_type": "formation_family",
            "label_value": {"family": family, "confidence": family_conf},
        }
    )

    # ── Formation strength (Phase 2) ──────────────────────────────────────
    strength = _classify_formation_strength(snap_positions)
    labels.append(
        {
            "label_type": "formation_strength",
            "label_value": {"strength": strength},
        }
    )

    # ── Personnel grouping (Phase 2) ──────────────────────────────────────
    personnel, pers_conf = _infer_personnel_grouping(snap_positions, analysis_tracklets)
    labels.append(
        {
            "label_type": "personnel_grouping",
            "label_value": {"grouping": personnel, "confidence": pers_conf},
        }
    )

    # ── Defensive front ───────────────────────────────────────────────────
    def_front, def_conf = _classify_defensive_front(snap_positions)
    labels.append(
        {
            "label_type": "defensive_front",
            "label_value": {"front": def_front, "confidence": def_conf},
        }
    )

    # ── Motion detection (enhanced with type) ─────────────────────────────
    motion_events = [e for e in events if e.get("event_type") == "motion_start"]
    motion_type = _classify_motion_type(
        motion_events, analysis_tracklets, snap_frame, fps
    )
    labels.append(
        {
            "label_type": "motion_detected",
            "label_value": {
                "detected": len(motion_events) > 0,
                "count": len(motion_events),
                "motion_type": motion_type,
            },
        }
    )

    # ── Shift detection (Phase 2) ─────────────────────────────────────────
    shift_detected, shift_conf = _detect_shift(
        analysis_tracklets, events, snap_frame, fps
    )
    labels.append(
        {
            "label_type": "shift_detected",
            "label_value": {"detected": shift_detected, "confidence": shift_conf},
        }
    )

    # ── Blitz candidate ───────────────────────────────────────────────────
    blitz, blitz_conf = _detect_blitz(
        snap_positions, analysis_tracklets, snap_frame, fps
    )
    labels.append(
        {
            "label_type": "blitz_candidate",
            "label_value": {"detected": blitz, "confidence": blitz_conf},
        }
    )

    # ── Field orientation ─────────────────────────────────────────────────
    # Which way the offence attacks in the *template* frame. This is a property
    # of the calibration, not of the play: the painted field is symmetric under
    # a 180° rotation, so the calibrate stage cannot tell its fit from the fit
    # rotated end-for-end, and every depth-behind-LOS feature downstream needs
    # the sign. Resolved from the players, and left unresolved when they do not
    # settle it -- the previous default was a silent +1.
    orientation = fo.estimate_over_frames(
        _positions_by_frame(analysis_tracklets, ORIENTATION_MAX_FRAMES)
    )
    labels.append(
        {
            "label_type": "field_orientation",
            "label_value": orientation.as_dict(),
        }
    )
    log.info(
        "stage_labels_field_orientation",
        clip_id=clip_id,
        direction=orientation.direction,
        evidence=orientation.evidence,
        agreement=orientation.agreement,
        frames_resolved=orientation.frames_resolved,
    )

    # Write labels to backend
    label_ids: list[str] = []
    for lbl in labels:
        check_control()
        source = "synthetic_test" if synthetic else "heuristic"
        lbl["label_value"].update(
            source=source, experimental=True, review_required=True,
            provenance={"source": source, "validated": False},
        )
        try:
            resp = backend.create_label(
                lbl["label_type"],
                lbl["label_value"],
                clip_id=clip_id,
                source="synthetic_test" if synthetic else "model",
            )
            label_ids.append(resp["id"])
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning(
                "label_write_failed", label_type=lbl.get("label_type"), error=str(exc)
            )
            raise

    team_label_ids = _write_team_labels(clip_id, team_by_tracklet, synthetic=synthetic)
    label_ids.extend(team_label_ids)

    log.info(
        "stage_labels_done",
        clip_id=clip_id,
        label_count=len(label_ids),
        team_classified_count=len(team_by_tracklet),
        official_tracklet_count=len(official_tracklet_ids),
    )
    return {
        "label_count": len(label_ids),
        "label_ids": label_ids,
        # Full label dicts for in-process consumers (render HUD reads
        # label_type/label_value without a backend round trip).
        "labels": labels,
        "tracklets": tracklets,
        "team_classification": {
            "classified_tracklets": len(team_by_tracklet),
            "official_tracklets": len(official_tracklet_ids),
            "sides": sides.evidence,
            "fallback": "no_visual_samples" if not team_by_tracklet else None,
            "limitations": [
                "requires bbox track_points and readable source frames",
                "labels are visual clusters, not roster-confirmed identities",
            ],
        },
    }


# ── Team color classification ─────────────────────────────────────────────────


def _classify_team_colors(
    tracklets: list[dict[str, Any]],
    *,
    snap_frame: int,
    input_uri: str | None,
    frames_by_number: dict[int, Any] | None,
    classifier: KMeansTeamClassifier | None = None,
) -> list[ClassificationResult]:
    """Run bounded k=3 CIELab classification when visual samples exist."""
    frames = frames_by_number or _load_team_frames(input_uri, tracklets, snap_frame)
    if not frames:
        log.info("team_classification_skipped", reason="no_frames")
        return []

    frame_classifications = classify_tracklet_frames(
        frames,
        tracklets,
        initial_frame=snap_frame,
        classifier=classifier,
    )
    return [
        result
        for frame_result in frame_classifications
        for result in frame_result.results
        if result.label != "unknown"
    ]


def _name_team_sides(
    results: list[ClassificationResult],
    tracklets: list[dict[str, Any]],
    classifier: KMeansTeamClassifier,
    context: SideContext,
) -> tuple[list[ClassificationResult], SideDecision]:
    """Turn the two team colour clusters into offense and defense.

    The classifier's own offense/defense names only say which colour is
    further from the grass. :mod:`pipeline.team_classification.sides`
    decides from the formation or the play-by-play; when neither can, both
    colours become unknown rather than a coin flip.
    """
    team_clusters = tuple(
        sorted(
            i
            for i, label in classifier.cluster_labels.items()
            if label in (TEAM_OFFENSE, TEAM_DEFENSE)
        )
    )
    if classifier.centroids is None or len(team_clusters) != 2:
        decision = SideDecision(None, "fewer_than_two_team_colours")
    else:
        votes: dict[str, dict[int, int]] = {}
        for result in results:
            if result.cluster_index in team_clusters:
                tally = votes.setdefault(result.tracklet_id, {})
                tally[result.cluster_index] = tally.get(result.cluster_index, 0) + 1
        cluster_by_tracklet = {tid: max(t, key=lambda c: t[c]) for tid, t in votes.items()}
        frames = sorted({r.frame_number for r in results})
        decision = resolve_offense_cluster(
            tracklets,
            cluster_by_tracklet,
            classifier.centroids,
            (team_clusters[0], team_clusters[1]),
            frames,
            context,
        )
    log.info(
        "team_sides_resolved",
        evidence=decision.evidence,
        resolved=decision.offense_cluster is not None,
        frames=decision.frames_resolved,
    )

    def side(result: ClassificationResult) -> ClassificationResult:
        if result.cluster_index not in team_clusters:
            return result
        if decision.offense_cluster is None:
            return replace(result, label="unknown", reason=f"{result.reason}:side_unresolved")
        label = TEAM_OFFENSE if result.cluster_index == decision.offense_cluster else TEAM_DEFENSE
        return replace(result, label=label, reason=f"{result.reason}:{decision.evidence}")

    named = [side(result) for result in results]
    return [r for r in named if r.label != "unknown"], decision


def _load_team_frames(
    input_uri: str | None,
    tracklets: list[dict[str, Any]],
    snap_frame: int,
) -> dict[int, Any]:
    if not input_uri:
        return {}

    wanted_frames = _wanted_team_frames(tracklets, snap_frame)
    if not wanted_frames:
        return {}

    video_path: Path | None = None
    delete_after = False
    try:
        local_path = Path(input_uri) if "://" not in input_uri else None
        if local_path is not None and local_path.exists():
            video_path = local_path
        else:
            video_path = object_store.download_to_temp(_uri_to_object_key(input_uri))
            delete_after = True

        from pipeline.hwaccel import nvdec_video_capture
        from pipeline.video_timeline import VideoTimeline, read_frames

        cap = nvdec_video_capture(video_path)
        try:
            # By timestamp: a frame seek lands several frames off on long
            # films, and colour samples must come from the box's own frame.
            return read_frames(cap, VideoTimeline.from_path(video_path), wanted_frames)
        finally:
            cap.release()
    except PipelineInterrupted:
        raise
    except Exception as exc:
        log.warning("team_classification_frame_load_failed", error=str(exc))
        return {}
    finally:
        if delete_after and video_path is not None:
            video_path.unlink(missing_ok=True)


def _wanted_team_frames(tracklets: list[dict[str, Any]], snap_frame: int) -> list[int]:
    """Frames to read for jersey colours: the snap first, then every track's own.

    Each track contributes up to :data:`TEAM_SAMPLES_PER_TRACK` frames spread
    over its life, so a fragment that starts after the snap still gets looks.
    Over the cap, every track keeps at least one frame (its busiest) and the
    frames that serve the most tracks fill the rest.
    """
    per_track: list[list[int]] = []
    on_screen: dict[int, int] = {}
    for tracklet in tracklets:
        frames = sorted(
            {
                int(point.get("frame_number", 0))
                for point in tracklet.get("track_points", [])
                if isinstance(point.get("bbox"), list) and len(point["bbox"]) == 4
            }
        )
        for frame in frames:
            on_screen[frame] = on_screen.get(frame, 0) + 1
        if frames:
            steps = min(TEAM_SAMPLES_PER_TRACK, len(frames))
            per_track.append(
                sorted({frames[round(i * (len(frames) - 1) / max(1, steps - 1))] for i in range(steps)})
            )
    if not per_track:
        return []

    chosen: list[int] = [snap_frame] if snap_frame in on_screen else []
    for frames in sorted(per_track, key=len):
        if len(chosen) >= TEAM_CLASSIFICATION_MAX_FRAMES:
            break
        if not any(frame in chosen for frame in frames):
            chosen.append(max(frames, key=lambda f: on_screen[f]))
    candidates = {frame for frames in per_track for frame in frames} - set(chosen)
    for frame in sorted(candidates, key=lambda f: (-on_screen[f], f)):
        if len(chosen) >= TEAM_CLASSIFICATION_MAX_FRAMES:
            break
        chosen.append(frame)
    head = chosen[:1] if chosen and chosen[0] == snap_frame else []
    return head + sorted(frame for frame in chosen if frame not in head)


def _aggregate_team_results(
    results: list[ClassificationResult],
) -> dict[str, ClassificationResult]:
    by_tracklet: dict[str, list[ClassificationResult]] = {}
    for result in results:
        by_tracklet.setdefault(result.tracklet_id, []).append(result)

    aggregated: dict[str, ClassificationResult] = {}
    for tracklet_id, tracklet_results in by_tracklet.items():
        labels = sorted({r.label for r in tracklet_results})
        best_label = max(
            labels,
            key=lambda label: (
                sum(1 for r in tracklet_results if r.label == label),
                sum(r.confidence for r in tracklet_results if r.label == label),
            ),
        )
        best_candidates = [r for r in tracklet_results if r.label == best_label]
        aggregated[tracklet_id] = max(best_candidates, key=lambda r: r.confidence)
    return aggregated


def _write_team_labels(
    clip_id: str,
    team_by_tracklet: dict[str, ClassificationResult],
    *,
    synthetic: bool = False,
) -> list[str]:
    written_ids: list[str] = []
    for tracklet_id, result in team_by_tracklet.items():
        backend.patch_tracklet_team_label(
            tracklet_id,
            result.label,
        )
        try:
            resp = backend.create_label(
                "team_classification",
                {
                    "team": result.label,
                    "confidence": result.confidence,
                    "frame_number": result.frame_number,
                    "reason": result.reason,
                    "stripe_score": round(result.stripe_score, 4),
                    "provenance": {"source": "synthetic_test" if synthetic else "heuristic", "validated": False},
                },
                clip_id=clip_id,
                tracklet_id=tracklet_id,
                source="synthetic_test" if synthetic else "model",
            )
            written_ids.append(resp["id"])
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning(
                "team_label_write_failed", tracklet_id=tracklet_id, error=str(exc)
            )
            raise
    return written_ids


def _uri_to_object_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


# ── Heuristic classifiers ─────────────────────────────────────────────────────


def _positions_at_frame(
    tracklets: list[dict[str, Any]], frame: int
) -> list[tuple[float, float]]:
    """Return (field_x, field_y) for every tracklet active at `frame`."""
    positions: list[tuple[float, float]] = []
    for t in tracklets:
        for pt in t.get("track_points", []):
            if pt.get("frame_number") == frame:
                fx = pt.get("field_x")
                fy = pt.get("field_y")
                if fx is not None and fy is not None:
                    positions.append((float(fx), float(fy)))
                break
    return positions


def _classify_offensive_formation(
    positions: list[tuple[float, float]],
) -> tuple[str, float]:
    """Classify offensive formation based on player alignment at snap."""
    if not positions:
        return ("unknown", 0.3)

    xs = sorted(p[0] for p in positions)
    los = xs[len(xs) // 2]

    near_los = sum(1 for x, _ in positions if abs(x - los) < 3)
    backfield = [p for p in positions if p[0] < los - 3]
    wide_left = sum(1 for _, y in positions if y < -10)
    wide_right = sum(1 for _, y in positions if y > 10)

    # Check for trips (3 receivers to one side)
    if wide_left >= 3 or wide_right >= 3:
        return ("trips", 0.65)

    # Check for empty (no backs, all receivers spread)
    if len(backfield) == 0 and (wide_left + wide_right) >= 4:
        return ("empty", 0.65)

    # Check for pistol (QB behind center, RB directly behind QB)
    if len(backfield) == 2:
        back_ys = [abs(b[1]) for b in backfield]
        if all(y < 3.0 for y in back_ys):
            return ("pistol", 0.6)

    # Under center vs shotgun
    if near_los >= 5 and len(backfield) >= 1:
        qb_depth = max(abs(b[0] - los) for b in backfield) if backfield else 0
        if qb_depth < 2.0:
            return ("under_center", 0.6)

    if len(backfield) >= 1:
        return ("shotgun", 0.6)

    return ("shotgun", 0.55)


def _classify_formation_family(
    positions: list[tuple[float, float]],
) -> tuple[str, float]:
    """Classify into broader formation family."""
    if not positions:
        return ("unknown", 0.3)

    xs = sorted(p[0] for p in positions)
    los = xs[len(xs) // 2]
    backfield = [p for p in positions if p[0] < los - 3]
    wide_players = sum(1 for _, y in positions if abs(y) > 10)

    if wide_players >= 4:
        return ("spread", 0.6)
    if len(backfield) >= 2:
        back_ys = sorted(b[1] for b in backfield)
        if len(back_ys) >= 2 and abs(back_ys[0] - back_ys[-1]) < 3:
            return ("i_form", 0.55)
    if wide_players >= 2 and len(backfield) >= 1:
        return ("pro", 0.55)
    if wide_players <= 1 and len(backfield) >= 2:
        return ("jumbo", 0.5)

    return ("singleback", 0.5)


def _classify_formation_strength(
    positions: list[tuple[float, float]],
) -> str:
    """Classify formation strength (boundary/field/balanced)."""
    if not positions:
        return "unknown"
    left_count = sum(1 for _, y in positions if y < -3)
    right_count = sum(1 for _, y in positions if y > 3)
    if abs(left_count - right_count) <= 1:
        return "balanced"
    if left_count > right_count:
        return "left"
    return "right"


def _infer_personnel_grouping(
    positions: list[tuple[float, float]],
    tracklets: list[dict[str, Any]],
) -> tuple[str, float]:
    """Infer personnel grouping (e.g. 11, 12, 21, 22)."""
    if not positions or len(positions) < 5:
        return ("unknown", 0.3)

    xs = sorted(p[0] for p in positions)
    los = xs[len(xs) // 2]

    # Count players in different zones
    backfield = sum(1 for x, _ in positions if x < los - 3)
    tight_end_zone = sum(1 for x, y in positions if abs(x - los) < 3 and abs(y) > 5)

    # Heuristic: RBs = backfield count minus QB (1)
    rb_count = max(0, backfield - 1)
    te_count = tight_end_zone

    # Sanity check: total skill should be <= 5
    rb_count = min(rb_count, 3)
    te_count = min(te_count, 3)

    grouping = f"{rb_count}{te_count}"
    return (grouping, 0.5)


def _classify_motion_type(
    motion_events: list[dict[str, Any]],
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
) -> str | None:
    """Classify motion type: jet, orbit, fly, trade, return."""
    if not motion_events:
        return None

    for event in motion_events:
        frame = event.get("frame_number", 0)
        # Find the tracklet with the most lateral movement around motion frame
        max_lateral = 0.0
        max_speed = 0.0
        for t in tracklets:
            pts = t.get("track_points", [])
            motion_pts = [
                p
                for p in pts
                if p.get("field_x") is not None
                and p.get("field_y") is not None
                and p.get("frame_number") is not None
                and abs(p["frame_number"] - frame) < fps * 0.5
            ]
            if len(motion_pts) < 2:
                continue
            y_start = motion_pts[0].get("field_y", 0)
            y_end = motion_pts[-1].get("field_y", 0)
            lateral = abs(float(y_end or 0) - float(y_start or 0))
            dt = motion_pts[-1].get("frame_number", 0) - motion_pts[0].get(
                "frame_number", 0
            )
            speed = lateral / max(dt / max(fps, 1), 0.01)
            if lateral > max_lateral:
                max_lateral = lateral
                max_speed = speed

        # Classify based on speed and distance
        if max_speed > 6.0 and max_lateral > 10.0:
            return "jet"
        if max_lateral > 15.0:
            return "orbit"
        if max_speed > 4.0:
            return "fly"
        if max_lateral > 5.0:
            return "trade"

    return "motion"


def _detect_shift(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
) -> tuple[bool, float]:
    """Detect pre-snap shift (multiple players moving simultaneously before snap)."""
    # Look at the window 1-3 seconds before snap
    window_start = max(0, snap_frame - int(fps * 3))
    window_end = snap_frame - int(fps * 0.5)

    if window_end <= window_start:
        return (False, 0.3)

    movers = 0
    for t in tracklets:
        pts = t.get("track_points", [])
        window_pts = [
            p
            for p in pts
            if p.get("field_x") is not None
            and p.get("field_y") is not None
            and p.get("frame_number") is not None
            and window_start <= p["frame_number"] <= window_end
        ]
        if len(window_pts) < 2:
            continue
        start_y = window_pts[0].get("field_y", 0)
        end_y = window_pts[-1].get("field_y", 0)
        if abs(float(end_y or 0) - float(start_y or 0)) > 3.0:
            movers += 1

    # Shift = 2+ players moving simultaneously (motion = just 1)
    if movers >= 2:
        return (True, min(0.8, 0.4 + movers * 0.1))

    return (False, 0.4)


def _classify_defensive_front(
    positions: list[tuple[float, float]],
) -> tuple[str, float]:
    """Heuristic: count defenders near LOS vs. in secondary."""
    if len(positions) < 6:
        return ("unknown", 0.3)
    xs = sorted(p[0] for p in positions)
    los = xs[len(xs) // 2]
    # Defenders should be on the far side of LOS
    near_los = sum(1 for x, _ in positions if 1 < abs(x - los) < 5)
    box_players = sum(1 for x, y in positions if 1 < abs(x - los) < 7 and abs(y) < 12)

    if near_los >= 5:
        return ("5-2", 0.55)
    if near_los >= 4:
        return ("4-3", 0.6)
    elif near_los == 3:
        return ("3-4", 0.6)
    if box_players >= 6:
        return ("dime", 0.5)
    if box_players >= 5:
        return ("nickel", 0.55)
    return ("quarters", 0.5)


def _detect_blitz(
    snap_positions: list[tuple[float, float]],
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
) -> tuple[bool, float]:
    """Detect blitz: defender(s) crossing LOS within ~0.5 s of snap."""
    if not snap_positions:
        return (False, 0.3)
    xs = sorted(p[0] for p in snap_positions)
    los = xs[len(xs) // 2]
    post_snap_frame = snap_frame + int(fps * 0.5)

    crossings = 0
    for t in tracklets:
        pts = {pt["frame_number"]: pt for pt in t.get("track_points", [])}
        snap_pt = pts.get(snap_frame)
        post_pt = pts.get(post_snap_frame)
        if (
            snap_pt
            and post_pt
            and snap_pt.get("field_x") is not None
            and snap_pt.get("field_y") is not None
            and post_pt.get("field_x") is not None
            and post_pt.get("field_y") is not None
        ):
            sx = snap_pt["field_x"]
            px = post_pt["field_x"]
            if abs(sx - los) < 6 and abs(px - los) < 2:
                crossings += 1

    return (crossings >= 2, min(0.9, 0.4 + crossings * 0.15))


def _positions_by_frame(
    tracklets: list[dict[str, Any]], max_frames: int
) -> list[list[tuple[float, float]]]:
    """Field positions grouped by frame, evenly sampled to ``max_frames``.

    Points without field coordinates are dropped rather than defaulted. The
    ``field_x or 0`` idiom this replaces turned an uncalibrated clip -- where
    every ``field_x`` is ``None`` -- into a frame full of players standing on
    the goal line, which is a coordinate, so nothing downstream could tell it
    from a measurement.
    """
    by_frame: dict[int, list[tuple[float, float]]] = {}
    for t in tracklets:
        for pt in t.get("track_points", []):
            fx, fy, fn = pt.get("field_x"), pt.get("field_y"), pt.get("frame_number")
            if fx is None or fy is None or fn is None:
                continue
            by_frame.setdefault(int(fn), []).append((float(fx), float(fy)))
    if not by_frame:
        return []
    frames = sorted(by_frame)
    if len(frames) > max_frames:
        step = len(frames) / max_frames
        frames = [frames[int(i * step)] for i in range(max_frames)]
    return [by_frame[f] for f in frames]
