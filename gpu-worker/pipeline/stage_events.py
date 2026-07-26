"""Stage 7 — Event Detection (Issues #132, #134).

Detects play events from the upstream pipeline artifacts. Two paths:

* **Multi-signal path (preferred).** When player ``tracklets`` (with field
  coordinates) are available, run the Bayesian snap detector + LOS estimator
  (Issue #132) and, when ball detections are present, the ball Kalman tracker
  + state machine + throw/catch attribution (Issue #134). Every event carries
  rich, *honestly-labelled* attributes — calibrated ``p_snap``, per-signal
  strengths, ``los_x`` / ``los_confidence``, ``throw_distance_yd``,
  ``hang_time_s``, ``qb_id`` / ``receiver_id`` — so downstream stages and
  coaches can consume and audit them.

* **Legacy heuristic path (fallback).** When no tracklets are supplied (older
  job payloads) the original bbox-displacement heuristic runs unchanged so the
  stage stays backward compatible.

Rich outputs are persisted in the existing ``events.attributes`` JSON column
(no schema migration); see ``docs/snap-los-ball-events.md``. Manual correction
is still expected — coaches can PATCH events via the API.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog

from pipeline import backend
from pipeline.ball import attribution as attrib
from pipeline.ball import trajectory_fitter as traj
from pipeline.ball.ball_state_machine import BallStateMachine
from pipeline.ball.ball_tracker import observations_from_detections, track_ball
from pipeline.events.los_estimator import LOSEstimate, LOSEstimator
from pipeline.events.snap_detector import BayesianSnapDetector, SnapInputs
from pipeline.homography.project import homography_from_flat

log = structlog.get_logger(__name__)

# Legacy heuristic tuning (fallback path only).
SNAP_MOTION_THRESHOLD = 15.0    # pixels of mean bbox displacement in one frame step
TACKLE_OVERLAP_THRESHOLD = 0.4  # IoU between two player bboxes → contact
STILLNESS_FRAMES = 10           # consecutive low-motion frames → end_of_play

# Minimum fused probability below which we do NOT emit a snap (avoid asserting
# a snap we cannot defend — see "no mock events as real" guardrail).
MIN_SNAP_CONFIDENCE = 0.2


def run(
    clip_id: str,
    detections: dict[str, list[dict[str, Any]]],
    fps: float,
    job_id: str,
    *,
    tracklets: list[dict[str, Any]] | None = None,
    ball_detections: dict[str, list[dict[str, Any]]] | None = None,
    pose_by_frame: dict[int, dict[str, dict[str, tuple[float, float]]]] | None = None,
    frames: list[np.ndarray] | None = None,
    frames_start_frame: int = 0,
    ol_track_ids: list[str] | None = None,
    qb_track_id: str | None = None,
    center_track_id: str | None = None,
    defender_ids: list[str] | None = None,
    homography: Any = None,
    los_band_px: tuple[int, int] | None = None,
    los_prior: dict[str, Any] | None = None,
    end_of_play_frame: int | None = None,
    goal_line_x: float | None = None,
) -> dict[str, Any]:
    """Detect events in a clip and persist them.

    All keyword arguments are optional: with only ``detections`` the legacy
    heuristic runs; supplying ``tracklets`` (and optionally ``ball_detections``,
    ``pose_by_frame``, ``homography``) activates the multi-signal path.

    ``homography`` is deliberately untyped: ``stage_calibrate`` serialises a flat
    nine-element list, the artifact sink round-trips whatever it is handed
    through JSON, and callers in tests pass a nested 3×3. ``homography_from_flat``
    accepts all three and returns ``None`` for anything unusable. The previous
    ``np.asarray(h) if h else None`` looked equivalent and was not: a flat list
    became shape ``(9,)``, and the first ``H @ [x, y, 1]`` in ``_project_obs``
    raised, failing the whole stage — so pose, labels and metrics downstream saw
    no events at all.
    """
    log.info("stage_events_start", clip_id=clip_id, multisignal=bool(tracklets))

    if tracklets:
        events, summary = _detect_multisignal(
            tracklets=tracklets,
            detections=detections,
            ball_detections=ball_detections,
            pose_by_frame=pose_by_frame,
            frames=frames,
            frames_start_frame=frames_start_frame,
            ol_track_ids=ol_track_ids,
            qb_track_id=qb_track_id,
            center_track_id=center_track_id,
            defender_ids=set(defender_ids) if defender_ids else None,
            homography=homography_from_flat(homography),
            los_band_px=los_band_px,
            los_prior=los_prior,
            end_of_play_frame=end_of_play_frame,
            goal_line_x=goal_line_x,
            fps=fps,
        )
    else:
        events = _detect_heuristic(detections, fps)
        summary = {}

    event_ids = _persist(clip_id, events)
    log.info(
        "stage_events_done",
        clip_id=clip_id,
        event_count=len(event_ids),
        **{k: v for k, v in summary.items() if k in ("snap_frame", "los_x")},
    )
    return {
        "event_count": len(event_ids),
        "event_ids": event_ids,
        "events": events,
        **summary,
    }


# ── Multi-signal path (Issues #132 + #134) ──────────────────────────────────


def _detect_multisignal(
    *,
    tracklets: list[dict[str, Any]],
    detections: dict[str, list[dict[str, Any]]],
    ball_detections: dict[str, list[dict[str, Any]]] | None,
    pose_by_frame: dict[int, dict[str, dict[str, tuple[float, float]]]] | None,
    frames: list[np.ndarray] | None,
    frames_start_frame: int,
    ol_track_ids: list[str] | None,
    qb_track_id: str | None,
    center_track_id: str | None,
    defender_ids: set[str] | None,
    homography: np.ndarray | None,
    los_band_px: tuple[int, int] | None,
    los_prior: dict[str, Any] | None,
    end_of_play_frame: int | None,
    goal_line_x: float | None,
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    player_positions = attrib.positions_by_frame(tracklets)

    # Ball field/pixel tracks (shared by the snap signal and the FSM).
    # Fall back to the merged detections artifact when no dedicated ball
    # detections artifact is present (stage_detect appends ball boxes into
    # detections rather than a separate key for most pipeline jobs).
    _ball_src = ball_detections if ball_detections is not None else detections
    px_obs = observations_from_detections(_ball_src) if _ball_src else {}
    field_obs = _project_obs(px_obs, homography) if (px_obs and homography is not None) else {}
    ball_track_field = track_ball(field_obs, fps) if field_obs else []
    ball_track_px = track_ball(px_obs, fps) if px_obs else []

    # ── Snap detection ─────────────────────────────────────────────────────
    snap_inputs = SnapInputs(
        tracklets=tracklets,
        fps=fps,
        frames=frames,
        frames_start_frame=frames_start_frame,
        pose_by_frame=pose_by_frame,
        ol_track_ids=ol_track_ids,
        qb_track_id=qb_track_id,
        los_band_px=los_band_px,
    )
    if center_track_id and field_obs:
        snap_inputs.ball_track = {p.frame: (p.x, p.y) for p in ball_track_field}
        snap_inputs.center_track = _field_track_for(tracklets, center_track_id)

    snap_result = BayesianSnapDetector().detect(snap_inputs)
    snap_frame = snap_result.snap_frame
    low_confidence = False
    if snap_frame is None:
        if snap_result.confidence >= MIN_SNAP_CONFIDENCE and snap_result.frame_probabilities:
            snap_frame = max(
                snap_result.frame_probabilities,
                key=lambda f: snap_result.frame_probabilities[f],
            )
            low_confidence = True
        else:
            log.warning("snap_not_detected", confidence=round(snap_result.confidence, 3))
            return events, {"snap_detected": False}

    # ── LOS estimation ─────────────────────────────────────────────────────
    ball_field_x = None
    if ball_track_field:
        at_snap = next((p for p in ball_track_field if p.frame == snap_frame), None)
        ball_field_x = at_snap.x if at_snap else None
    los = LOSEstimator().estimate(tracklets, snap_frame, ball_field_x=ball_field_x)
    if los_prior:
        prior = LOSEstimate(
            float(los_prior.get("los_x", 50.0)),
            float(los_prior.get("confidence", 0.0)),
            "bayesian",
        )
        los = LOSEstimator().propagate(prior, los, gain_yards=float(los_prior.get("gain_yards", 0.0)))

    events.append({
        "event_type": "snap",
        "frame_number": snap_frame,
        "timestamp_seconds": snap_frame / fps,
        "attributes": {
            "p_snap": snap_result.confidence,
            "low_confidence": low_confidence,
            "signals_present": snap_result.signals_present,
            "signal_strengths": snap_result.explain(snap_frame),
            "los_x": round(los.los_x, 3),
            "los_confidence": round(los.confidence, 3),
            "los_method": los.method,
            "los_support": los.support,
            "source": "model",
        },
    })
    summary: dict[str, Any] = {
        "snap_detected": True,
        "snap_frame": snap_frame,
        "snap_confidence": snap_result.confidence,
        "los_x": round(los.los_x, 3),
        "los_confidence": round(los.confidence, 3),
    }

    # ── Ball state machine + attribution (Issue #134) ───────────────────────
    if ball_track_field:
        sm = BallStateMachine().run(
            snap_frame,
            ball_track_field,
            player_positions,
            qb_id=qb_track_id,
            defender_ids=defender_ids,
            end_of_play_frame=end_of_play_frame,
            goal_line_x=goal_line_x,
        )
        qb_id = (
            attrib.attribute_throw(ball_track_field, player_positions, sm.first_airborne_frame)
            if sm.first_airborne_frame is not None
            else None
        )
        if sm.catch_frame is not None:
            player_velocities = _compute_player_velocities(
                player_positions, sm.catch_frame, fps
            )
            receiver_id = attrib.attribute_catch(
                ball_track_field,
                player_positions,
                sm.catch_frame,
                player_velocities=player_velocities,
            )
        else:
            receiver_id = None
        traj_metrics = _trajectory_metrics(
            sm.throw_frame, sm.catch_frame, ball_track_px, ball_track_field, homography, fps
        )
        for ev in sm.events():
            if ev["event_type"] == "snap":
                continue  # already emitted with LOS attributes
            attrs: dict[str, Any] = {"ball_state": ev.get("state"), "source": "model"}
            if ev["event_type"] == "throw":
                attrs["qb_id"] = qb_id
                attrs.update(traj_metrics)
            if ev["event_type"] in ("catch", "interception"):
                attrs["receiver_id"] = receiver_id
                attrs.update(traj_metrics)
            events.append({
                "event_type": ev["event_type"],
                "frame_number": ev["frame_number"],
                "timestamp_seconds": int(ev["frame_number"]) / fps,
                "attributes": attrs,
            })
        summary["ball_states"] = [s.value for s in sm.states]
        summary["ball_state_valid"] = _validate_states(sm.states)
        if qb_id is not None:
            summary["qb_id"] = qb_id
        if receiver_id is not None:
            summary["receiver_id"] = receiver_id

    return events, summary


def _trajectory_metrics(
    throw_frame: int | None,
    catch_frame: int | None,
    ball_track_px: list[Any],
    ball_track_field: list[Any],
    homography: np.ndarray | None,
    fps: float,
) -> dict[str, Any]:
    """Throw distance, hang time, and apex for a completed/airborne pass."""
    metrics: dict[str, Any] = {}
    if throw_frame is None or catch_frame is None:
        return metrics
    metrics["hang_time_s"] = round(traj.hang_time_seconds(throw_frame, catch_frame, fps), 3)

    px_by_frame = {p.frame: (p.x, p.y) for p in ball_track_px}
    if homography is not None and throw_frame in px_by_frame and catch_frame in px_by_frame:
        metrics["throw_distance_yd"] = round(
            traj.field_plane_distance(homography, px_by_frame[throw_frame], px_by_frame[catch_frame]),
            2,
        )

    times, ys = [], []
    for p in ball_track_px:
        if throw_frame <= p.frame <= catch_frame:
            times.append(p.frame / fps)
            ys.append(p.y)
    fit = traj.fit_parabola(times, ys) if len(times) >= 3 else None
    if fit is not None and fit.well_formed:
        metrics["apex_y_image"] = round(fit.apex_y, 2)
        metrics["trajectory_rms_px"] = round(fit.rms_residual, 2)
    return metrics


def _project_obs(
    px_obs: dict[int, tuple[float, float]], homography: np.ndarray
) -> dict[int, tuple[float, float]]:
    return {f: traj.apply_homography(homography, pt) for f, pt in px_obs.items()}


def _field_track_for(
    tracklets: list[dict[str, Any]], track_id: str
) -> dict[int, tuple[float, float]]:
    for t in tracklets:
        if str(t.get("id")) == str(track_id):
            return {
                int(pt["frame_number"]): (float(pt["field_x"]), float(pt["field_y"]))
                for pt in t.get("track_points", [])
                if pt.get("field_x") is not None and pt.get("field_y") is not None
            }
    return {}


def _validate_states(states: list[Any]) -> bool:
    from pipeline.ball.ball_state_machine import validate

    return validate(states)


def _compute_player_velocities(
    player_positions: dict[int, dict[str, tuple[float, float]]],
    catch_frame: int | None,
    fps: float,
) -> dict[str, tuple[float, float]] | None:
    """Estimate player velocity (yd/s) at *catch_frame* from adjacent frames.

    Returns ``None`` when there is no previous frame to diff against, so the
    caller can omit ``player_velocities`` and fall back to nearest-player
    attribution unchanged.
    """
    if catch_frame is None:
        return None
    prev_frames = [f for f in player_positions if f < catch_frame]
    if not prev_frames:
        return None
    prev_frame = max(prev_frames)
    dt = (catch_frame - prev_frame) / fps if fps else 0.0
    if dt <= 0.0:
        return None
    cur = player_positions.get(catch_frame, {})
    prev = player_positions.get(prev_frame, {})
    velocities: dict[str, tuple[float, float]] = {}
    for tid, (cx, cy) in cur.items():
        if tid in prev:
            px, py = prev[tid]
            velocities[tid] = ((cx - px) / dt, (cy - py) / dt)
    return velocities or None


# ── Legacy heuristic path (unchanged behaviour) ─────────────────────────────


def _detect_heuristic(
    detections: dict[str, list[dict[str, Any]]], fps: float
) -> list[dict[str, Any]]:
    sorted_frames = sorted(int(f) for f in detections)
    events: list[dict[str, Any]] = []

    prev_centroids: list[tuple[float, float]] = []
    in_motion = False
    motion_start_frame = 0
    low_motion_streak = 0
    snap_detected = False

    for frame_idx in sorted_frames:
        frame_dets = [d for d in detections.get(str(frame_idx), [])
                      if d.get("class") == "player"]
        centroids = [_centroid(d["bbox"]) for d in frame_dets]

        if prev_centroids and centroids:
            mean_disp = _mean_displacement(prev_centroids, centroids)

            if not snap_detected and mean_disp > SNAP_MOTION_THRESHOLD:
                snap_detected = True
                events.append({
                    "event_type": "snap",
                    "frame_number": frame_idx,
                    "timestamp_seconds": frame_idx / fps,
                    "attributes": {"mean_displacement": mean_disp, "source": "model"},
                })

            if not snap_detected:
                if not in_motion and mean_disp > SNAP_MOTION_THRESHOLD * 0.4:
                    in_motion = True
                    motion_start_frame = frame_idx
                    events.append({
                        "event_type": "motion_start",
                        "frame_number": frame_idx,
                        "timestamp_seconds": frame_idx / fps,
                        "attributes": {},
                    })
                elif in_motion and mean_disp < SNAP_MOTION_THRESHOLD * 0.2:
                    in_motion = False
                    events.append({
                        "event_type": "motion_end",
                        "frame_number": frame_idx,
                        "timestamp_seconds": frame_idx / fps,
                        "attributes": {"duration_frames": frame_idx - motion_start_frame},
                    })

            if snap_detected:
                bboxes = [d["bbox"] for d in frame_dets]
                for i in range(len(bboxes)):
                    for j in range(i + 1, len(bboxes)):
                        iou = _iou(bboxes[i], bboxes[j])
                        if iou > TACKLE_OVERLAP_THRESHOLD:
                            events.append({
                                "event_type": "contact",
                                "frame_number": frame_idx,
                                "timestamp_seconds": frame_idx / fps,
                                "attributes": {"iou": iou},
                            })

            if snap_detected:
                if mean_disp < SNAP_MOTION_THRESHOLD * 0.15:
                    low_motion_streak += 1
                else:
                    low_motion_streak = 0
                if low_motion_streak == STILLNESS_FRAMES:
                    events.append({
                        "event_type": "end_of_play",
                        "frame_number": frame_idx,
                        "timestamp_seconds": frame_idx / fps,
                        "attributes": {},
                    })
                    snap_detected = False
                    low_motion_streak = 0

        prev_centroids = centroids

    return events


def _persist(clip_id: str, events: list[dict[str, Any]]) -> list[str]:
    event_ids: list[str] = []
    for ev in events:
        try:
            resp = backend.create_event(
                clip_id,
                ev["event_type"],
                frame_number=ev.get("frame_number"),
                timestamp_seconds=ev.get("timestamp_seconds"),
                attributes=ev.get("attributes"),
            )
            event_ids.append(resp["id"])
        except Exception as exc:
            log.warning("event_write_failed", event_type=ev.get("event_type"), error=str(exc))
    return event_ids


# ── Helpers ───────────────────────────────────────────────────────────────────


def _centroid(bbox: list[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def _mean_displacement(
    prev: list[tuple[float, float]], curr: list[tuple[float, float]]
) -> float:
    """Mean Euclidean distance between nearest-neighbour centroid pairs."""
    if not prev or not curr:
        return 0.0
    total = 0.0
    n = min(len(prev), len(curr))
    for i in range(n):
        dx = curr[i][0] - prev[i][0]
        dy = curr[i][1] - prev[i][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total / n


def _iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / (area_a + area_b - inter)
