"""Stage: Pose-Lite Biomechanics (Issue #6).

Implements coarse pose-based biomechanics from RTMPose/ViTPose keypoints.
All metrics are written with ``experimental_flag=True`` and
``analytics_safe=False`` — they remain hidden from all dashboards until a
position coach explicitly approves them via ``POST /api/v1/metrics/{id}/reviews``.

Position-group dispatch:
  OL / DL  — pad_level, torso_angle, block_shed_timing, pass-set weight distribution
  WR       — breakpoint mechanics (shoulder-over-knee, decel steps, hip sink,
             pre-snap stance, release technique)
  QB       — stride consistency, shoulder-hip separation, weight transfer,
             release-point consistency
  ALL      — stride symmetry (left/right ratio), biomechanical drift trend

Video constraints:
  - No .mp4 files are required at import time.
  - All metric computation runs on keypoint sequences (list[list[dict]]) regardless
    of how they were obtained.
  - Pass a ``MockVideoSource`` / ``StubPoseEstimator`` from the sibling modules to
    run everything in unit tests without real footage.

Metric names (written into the ``metrics`` table):
  pose_pad_level
  pose_block_shed_timing
  pose_pass_set_weight_distribution
  pose_wr_shoulder_over_knee
  pose_wr_deceleration_steps
  pose_wr_hip_sink_depth
  pose_wr_pre_snap_stance
  pose_wr_release_technique
  pose_qb_stride_consistency
  pose_qb_shoulder_hip_separation
  pose_qb_weight_transfer
  pose_qb_release_consistency
  pose_stride_symmetry
  pose_biomechanical_drift
  pose_body_orientation_proxy
"""

from __future__ import annotations

import math
import statistics
from typing import Any

import structlog

from pipeline import backend
from pipeline.metrics.gait_asymmetry import compute_gait_asymmetry
from pipeline.pose.head_yaw import compute_head_yaw, summarize_body_orientation
from pipeline.pose_estimator import (
    angle_degrees,
    get_estimator,
    kp,
    midpoint,
    vector_angle_from_vertical,
)
from pipeline.video_ingest import VideoSource

log = structlog.get_logger(__name__)

# ── Public metric name constants ──────────────────────────────────────────────
POSE_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "pose_pad_level",
        "pose_block_shed_timing",
        "pose_pass_set_weight_distribution",
        "pose_wr_shoulder_over_knee",
        "pose_wr_deceleration_steps",
        "pose_wr_hip_sink_depth",
        "pose_wr_pre_snap_stance",
        "pose_wr_release_technique",
        "pose_qb_stride_consistency",
        "pose_qb_shoulder_hip_separation",
        "pose_qb_weight_transfer",
        "pose_qb_release_consistency",
        "pose_stride_symmetry",
        "pose_biomechanical_drift",
        "pose_body_orientation_proxy",
    }
)

# Threshold for injury-risk flag in stride symmetry
_ASYMMETRY_RISK_THRESHOLD = 0.15

# Frame-stride for pose estimation (1-in-3 frames balances accuracy vs speed)
_POSE_FRAME_STRIDE = 3


# ── Entry point ───────────────────────────────────────────────────────────────


def run(
    clip_id: str,
    video_source: VideoSource,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    job_id: str,
    model_path: str | None = None,
) -> dict[str, Any]:
    """Run the pose-lite biomechanics stage for a clip.

    1. Extract frames from ``video_source`` at 1-in-3 stride.
    2. Run pose estimation on each frame (real or stub).
    3. Accumulate per-frame keypoints into per-tracklet sequences.
    4. Compute biomechanics metrics by position group.
    5. Write raw keypoints + computed metrics to the backend.

    All output metrics are written with ``experimental_flag=True``.

    Args:
        clip_id:       DB UUID of the clip being processed.
        video_source:  Any VideoSource (local .mp4, R2, or Mock).
        tracklets:     List of tracklet dicts from the track stage.
        events:        List of event dicts (snap, throw, handoff, contact, …).
        analytics_safe: Whether field calibration passed; passed through to
                         suppression logic (pose metrics are never suppressed
                         purely on calibration — they have their own flag).
        fps:           Video frame rate.
        job_id:        Processing job UUID for lineage.
        model_path:    Path to RTMPose .pth weights, or ``None`` for stub.

    Returns:
        ``{"keypoint_count": int, "metric_count": int, "metric_ids": list[str]}``
    """
    log.info("stage_pose_start", clip_id=clip_id, tracklet_count=len(tracklets))

    estimator = get_estimator(model_path)

    # ── Build frame → keypoints map ───────────────────────────────────────────
    frame_keypoints: dict[int, list[dict[str, Any]]] = {}
    for frame_num, frame in video_source.iter_frames(stride=_POSE_FRAME_STRIDE):
        frame_keypoints[frame_num] = estimator.estimate(frame)

    total_frames_processed = len(frame_keypoints)
    log.info("stage_pose_frames_processed", count=total_frames_processed)

    if not frame_keypoints and not tracklets:
        log.info("stage_pose_no_data", clip_id=clip_id)
        return {"keypoint_count": 0, "metric_count": 0, "metric_ids": [], "pose_metrics": {}}

    # ── Resolve key events ────────────────────────────────────────────────────
    snap_frame = _event_frame(events, "snap")
    throw_frame = _event_frame(events, "throw")
    contact_frame = _event_frame(events, "contact")

    # ── Per-tracklet processing ───────────────────────────────────────────────
    keypoint_count = 0
    metrics: list[dict[str, Any]] = []
    # Per-tracklet gait summaries handed to the chained metrics job (#149) so
    # workload fusion can fold asymmetry in without re-reading keypoints.
    pose_metrics: dict[str, dict[str, Any]] = {}

    for tracklet in tracklets:
        tracklet_id = tracklet.get("id")
        position_group = (tracklet.get("position_group") or "").upper()

        # Collect the keypoint sequence for this tracklet's frame range
        t_start = tracklet.get("start_frame", 0)
        t_end = tracklet.get("end_frame", total_frames_processed * _POSE_FRAME_STRIDE)
        kp_seq = _build_keypoint_sequence(frame_keypoints, t_start, t_end)

        if not kp_seq:
            continue

        # Write raw keypoints to pose_keypoints table
        kp_ids = _write_keypoints(clip_id, tracklet_id, kp_seq, job_id)
        keypoint_count += len(kp_ids)

        orientation_metric = _compute_body_orientation_proxy(
            kp_seq,
            tracklet_id,
            player_id=tracklet.get("player_id"),
            position_group=position_group or None,
        )
        if orientation_metric:
            metrics.append(orientation_metric)

        # Position-group-specific biomechanics
        if position_group in ("OL", "DL", "OT", "OG", "OC", "DE", "DT", "NT"):
            metrics.extend(_compute_pad_level(kp_seq, tracklet_id))
            metrics.extend(_compute_pass_set_weight_distribution(kp_seq, tracklet_id))
            if contact_frame is not None:
                metrics.extend(_compute_block_shed_timing(kp_seq, contact_frame, fps, tracklet_id))

        elif position_group in ("WR", "TE", "SLOT"):
            metrics.extend(_compute_wr_breakpoint(kp_seq, snap_frame or 0, fps, tracklet_id))

        elif position_group in ("QB",):
            metrics.extend(
                _compute_qb_mechanics(kp_seq, snap_frame or 0, throw_frame, fps, tracklet_id)
            )

        # All players: stride symmetry
        stride_metric = _compute_stride_symmetry(kp_seq, fps, tracklet_id)
        if stride_metric:
            metrics.append(stride_metric)
            gait_summary = stride_metric.get("gait_summary")
            if tracklet_id is not None and gait_summary:
                pose_metrics[str(tracklet_id)] = dict(gait_summary)

    # ── Biomechanical drift (across all OL tracklets in clip) ─────────────────
    ol_tracklets = [
        t for t in tracklets
        if (t.get("position_group") or "").upper() in ("OL", "DL", "OT", "OG", "OC", "DE", "DT")
    ]
    if len(ol_tracklets) >= 2:
        drift = _compute_biomechanical_drift(ol_tracklets, frame_keypoints)
        if drift:
            metrics.append(drift)

    # ── Write metrics to backend ──────────────────────────────────────────────
    metric_ids: list[str] = []
    for m in metrics:
        try:
            payload: dict[str, Any] = {
                "clip_id": clip_id,
                "tracklet_id": m.get("tracklet_id"),
                "metric_name": m["metric_name"],
                "metric_value": m["metric_value"],
                "unit": m.get("unit"),
                "experimental_flag": True,
                "analytics_safe": False,
                "confidence": m.get("confidence"),
                "asymmetry_index": m.get("asymmetry_index"),
                "job_id": job_id,
            }
            resp = backend.create_metric(**payload)
            metric_ids.append(resp["id"])
        except Exception as exc:
            log.warning("pose_metric_write_failed", name=m.get("metric_name"), error=str(exc))

    log.info(
        "stage_pose_done",
        clip_id=clip_id,
        keypoint_count=keypoint_count,
        metric_count=len(metric_ids),
    )
    return {
        "keypoint_count": keypoint_count,
        "metric_count": len(metric_ids),
        "metric_ids": metric_ids,
        "pose_metrics": pose_metrics,
    }


# ── OL / DL — Pad Level & Leverage ────────────────────────────────────────────


def _compute_pad_level(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    tracklet_id: str | None,
) -> list[dict[str, Any]]:
    """Compute hip flexion and torso angle for OL/DL pad level assessment.

    Hip flexion angle = angle at hip joint in hip→knee→ankle chain.
    Torso angle       = angle of hip-centre → shoulder-centre vector vs. vertical.

    Lower torso angle = more upright (pass-set); higher = more forward lean (drive block).
    """
    hip_flexions: list[float] = []
    torso_angles: list[float] = []
    confs: list[float] = []

    for entry in kp_seq:
        keypoints = entry["keypoints"]

        # Left side hip flexion
        lhip = kp(keypoints, "left_hip")
        lknee = kp(keypoints, "left_knee")
        lankle = kp(keypoints, "left_ankle")
        rhip = kp(keypoints, "right_hip")
        rknee = kp(keypoints, "right_knee")
        rankle = kp(keypoints, "right_ankle")
        lshoulder = kp(keypoints, "left_shoulder")
        rshoulder = kp(keypoints, "right_shoulder")

        min_conf = min(lhip["confidence"], lknee["confidence"], lankle["confidence"],
                       rhip["confidence"], rknee["confidence"], rankle["confidence"])
        if min_conf < 0.3:
            continue

        left_flex = angle_degrees(
            (lhip["x"], lhip["y"]),
            (lknee["x"], lknee["y"]),
            (lankle["x"], lankle["y"]),
        )
        right_flex = angle_degrees(
            (rhip["x"], rhip["y"]),
            (rknee["x"], rknee["y"]),
            (rankle["x"], rankle["y"]),
        )
        hip_flexion = (left_flex + right_flex) / 2.0

        hip_centre = midpoint((lhip["x"], lhip["y"]), (rhip["x"], rhip["y"]))
        shoulder_centre = midpoint(
            (lshoulder["x"], lshoulder["y"]),
            (rshoulder["x"], rshoulder["y"]),
        )
        torso_angle = vector_angle_from_vertical(hip_centre, shoulder_centre)

        hip_flexions.append(hip_flexion)
        torso_angles.append(torso_angle)
        confs.append(min_conf)

    if not hip_flexions:
        return []

    mean_hip_flex = statistics.mean(hip_flexions)
    mean_torso = statistics.mean(torso_angles)
    confidence = statistics.mean(confs)

    return [
        {
            "metric_name": "pose_pad_level",
            "metric_value": {
                "hip_flexion_degrees": round(mean_hip_flex, 1),
                "torso_angle_degrees": round(mean_torso, 1),
                "sample_frames": len(hip_flexions),
                "confidence": round(confidence, 3),
            },
            "unit": "degrees",
            "confidence": round(confidence, 3),
            "tracklet_id": tracklet_id,
        }
    ]


def _compute_pass_set_weight_distribution(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    tracklet_id: str | None,
) -> list[dict[str, Any]]:
    """Classify pass-set vs run-block intent from torso angle.

    Forward lean (mean torso_angle > 20°) → run block intent.
    Upright (torso_angle ≤ 20°) → pass set.
    """
    torso_angles: list[float] = []
    confs: list[float] = []

    for entry in kp_seq:
        keypoints = entry["keypoints"]
        lhip = kp(keypoints, "left_hip")
        rhip = kp(keypoints, "right_hip")
        lshoulder = kp(keypoints, "left_shoulder")
        rshoulder = kp(keypoints, "right_shoulder")

        min_conf = min(lhip["confidence"], rhip["confidence"],
                       lshoulder["confidence"], rshoulder["confidence"])
        if min_conf < 0.3:
            continue

        hip_c = midpoint((lhip["x"], lhip["y"]), (rhip["x"], rhip["y"]))
        shoulder_c = midpoint(
            (lshoulder["x"], lshoulder["y"]),
            (rshoulder["x"], rshoulder["y"]),
        )
        torso_angles.append(vector_angle_from_vertical(hip_c, shoulder_c))
        confs.append(min_conf)

    if not torso_angles:
        return []

    mean_torso = statistics.mean(torso_angles)
    confidence = statistics.mean(confs)
    classification = "run_block" if mean_torso > 20.0 else "pass_set"

    return [
        {
            "metric_name": "pose_pass_set_weight_distribution",
            "metric_value": {
                "classification": classification,
                "forward_lean_score": round(min(mean_torso / 45.0, 1.0), 3),
                "mean_torso_angle_degrees": round(mean_torso, 1),
                "confidence": round(confidence, 3),
            },
            "unit": "category",
            "confidence": round(confidence, 3),
            "tracklet_id": tracklet_id,
        }
    ]


def _compute_block_shed_timing(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    contact_frame: int,
    fps: float,
    tracklet_id: str | None,
) -> list[dict[str, Any]]:
    """Estimate block shed timing: frames from contact until DL torso returns to upright.

    «Upright» = torso_angle < 15° after contact.
    """
    post_contact = [e for e in kp_seq if e["frame_number"] >= contact_frame]
    if len(post_contact) < 2:
        return []

    shed_frame: int | None = None
    shed_conf = 0.0
    for entry in post_contact:
        keypoints = entry["keypoints"]
        lhip = kp(keypoints, "left_hip")
        rhip = kp(keypoints, "right_hip")
        lshoulder = kp(keypoints, "left_shoulder")
        rshoulder = kp(keypoints, "right_shoulder")
        min_conf = min(lhip["confidence"], rhip["confidence"],
                       lshoulder["confidence"], rshoulder["confidence"])
        if min_conf < 0.3:
            continue
        hip_c = midpoint((lhip["x"], lhip["y"]), (rhip["x"], rhip["y"]))
        shoulder_c = midpoint(
            (lshoulder["x"], lshoulder["y"]),
            (rshoulder["x"], rshoulder["y"]),
        )
        torso_angle = vector_angle_from_vertical(hip_c, shoulder_c)
        if torso_angle < 15.0:
            shed_frame = entry["frame_number"]
            shed_conf = min_conf
            break

    if shed_frame is None:
        return []

    shed_seconds = (shed_frame - contact_frame) / max(fps, 1.0)
    return [
        {
            "metric_name": "pose_block_shed_timing",
            "metric_value": {
                "seconds": round(shed_seconds, 3),
                "shed_frame": shed_frame,
                "confidence": round(shed_conf, 3),
            },
            "unit": "s",
            "confidence": round(shed_conf, 3),
            "tracklet_id": tracklet_id,
        }
    ]


# ── WR — Breakpoint Mechanics ──────────────────────────────────────────────────


def _compute_wr_breakpoint(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    snap_frame: int,
    fps: float,
    tracklet_id: str | None,
) -> list[dict[str, Any]]:
    """Compute all WR breakpoint mechanics metrics."""
    metrics: list[dict[str, Any]] = []

    # Pre-snap stance (first frame only)
    if kp_seq:
        pre_snap = _wr_pre_snap_stance(kp_seq[0]["keypoints"], tracklet_id)
        if pre_snap:
            metrics.append(pre_snap)

    # Breakpoint analysis: look for the highest-y-displacement point (route top)
    route_top_idx = _find_route_top(kp_seq)
    if route_top_idx is not None and route_top_idx < len(kp_seq):
        route_kps = kp_seq[route_top_idx]["keypoints"]

        # Shoulder-over-knee at route top
        soc = _wr_shoulder_over_knee(route_kps, tracklet_id)
        if soc:
            metrics.append(soc)

        # Deceleration steps (frames before route top)
        pre_break = kp_seq[max(0, route_top_idx - int(fps * 0.5)):route_top_idx + 1]
        decel = _wr_deceleration_steps(pre_break, fps, tracklet_id)
        if decel:
            metrics.append(decel)

        # Hip sink depth
        sink = _wr_hip_sink_depth(pre_break, fps, tracklet_id)
        if sink:
            metrics.append(sink)

    # Release technique (classification from first 10 frames after snap)
    snap_window = [e for e in kp_seq if snap_frame <= e["frame_number"] <= snap_frame + int(fps * 0.4)]
    if snap_window:
        release = _wr_release_technique(snap_window, tracklet_id)
        if release:
            metrics.append(release)

    return metrics


def _find_route_top(kp_seq: list[dict[str, list[dict[str, Any]]]]) -> int | None:
    """Find the index of the frame where the WR is furthest down-field (max x displacement)."""
    if not kp_seq:
        return None
    xs: list[float] = []
    for entry in kp_seq:
        hip_l = kp(entry["keypoints"], "left_hip")
        hip_r = kp(entry["keypoints"], "right_hip")
        xs.append((hip_l["x"] + hip_r["x"]) / 2.0)
    if not xs:
        return None
    return int(max(range(len(xs)), key=lambda i: xs[i]))


def _wr_pre_snap_stance(
    keypoints: list[dict[str, Any]], tracklet_id: str | None
) -> dict[str, Any] | None:
    """Classify pre-snap stance: 80/20 or 50/50 weight distribution.

    Heuristic: if one foot is significantly behind the other, it is 80/20 (drive foot back).
    """
    la = kp(keypoints, "left_ankle")
    ra = kp(keypoints, "right_ankle")
    if la["confidence"] < 0.3 or ra["confidence"] < 0.3:
        return None

    x_diff = abs(la["x"] - ra["x"])
    frame_w_est = 1280.0
    relative_offset = x_diff / frame_w_est

    classification = "80_20" if relative_offset > 0.04 else "50_50"
    return {
        "metric_name": "pose_wr_pre_snap_stance",
        "metric_value": {
            "weight_distribution": classification,
            "foot_x_offset_ratio": round(relative_offset, 4),
            "confidence": round((la["confidence"] + ra["confidence"]) / 2.0, 3),
        },
        "unit": "category",
        "confidence": round((la["confidence"] + ra["confidence"]) / 2.0, 3),
        "tracklet_id": tracklet_id,
    }


def _wr_shoulder_over_knee(
    keypoints: list[dict[str, Any]], tracklet_id: str | None
) -> dict[str, Any] | None:
    """Check shoulder-over-knee alignment at route top (overextension check)."""
    ls = kp(keypoints, "left_shoulder")
    rs = kp(keypoints, "right_shoulder")
    lk = kp(keypoints, "left_knee")
    rk = kp(keypoints, "right_knee")

    min_conf = min(ls["confidence"], rs["confidence"], lk["confidence"], rk["confidence"])
    if min_conf < 0.3:
        return None

    shoulder_x = (ls["x"] + rs["x"]) / 2.0
    knee_x = (lk["x"] + rk["x"]) / 2.0
    offset = shoulder_x - knee_x
    # Positive offset = shoulder ahead of knee = overextension = slower COD
    overextended = offset > 10.0

    return {
        "metric_name": "pose_wr_shoulder_over_knee",
        "metric_value": {
            "shoulder_knee_offset_px": round(offset, 1),
            "overextended": overextended,
            "confidence": round(min_conf, 3),
        },
        "unit": "px",
        "confidence": round(min_conf, 3),
        "tracklet_id": tracklet_id,
    }


def _wr_deceleration_steps(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    fps: float,
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Count deceleration steps leading into the route break.

    Detects foot-contact events by local ankle-y minima (foot closest to ground).
    Elite = 2–3 steps.
    """
    if len(kp_seq) < 3:
        return None

    ankle_ys: list[float] = []
    for entry in kp_seq:
        la = kp(entry["keypoints"], "left_ankle")
        ra = kp(entry["keypoints"], "right_ankle")
        ankle_ys.append(max(la["y"], ra["y"]))

    # Count local maxima in y (in image space, higher y = closer to ground)
    step_count = 0
    for i in range(1, len(ankle_ys) - 1):
        if ankle_ys[i] > ankle_ys[i - 1] and ankle_ys[i] > ankle_ys[i + 1]:
            step_count += 1

    elite = 2 <= step_count <= 3
    avg_conf = statistics.mean(
        (kp(e["keypoints"], "left_ankle")["confidence"]
         + kp(e["keypoints"], "right_ankle")["confidence"]) / 2.0
        for e in kp_seq
    )

    return {
        "metric_name": "pose_wr_deceleration_steps",
        "metric_value": {
            "step_count": step_count,
            "elite": elite,
            "confidence": round(avg_conf, 3),
        },
        "unit": "count",
        "confidence": round(avg_conf, 3),
        "tracklet_id": tracklet_id,
    }


def _wr_hip_sink_depth(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    fps: float,
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Compute hip sink depth (vertical drop of CoM) at the route break."""
    if len(kp_seq) < 2:
        return None

    hip_ys: list[float] = []
    confs: list[float] = []
    filtered_frame_numbers: list[int] = []
    for entry in kp_seq:
        lh = kp(entry["keypoints"], "left_hip")
        rh = kp(entry["keypoints"], "right_hip")
        min_conf = min(lh["confidence"], rh["confidence"])
        if min_conf < 0.3:
            continue
        hip_ys.append((lh["y"] + rh["y"]) / 2.0)
        confs.append(min_conf)
        filtered_frame_numbers.append(entry["frame_number"])

    if len(hip_ys) < 2:
        return None

    # In image space y increases downward; higher y = lower CoM = sink
    min_y = min(hip_ys)
    max_y = max(hip_ys)
    depth_px = max_y - min_y
    # Approximate: 1280px wide ≈ 13.3 yd field width → 1 px ≈ 0.0104 yd
    depth_yards = depth_px * (13.3 / 1280.0)

    # Velocity estimate: depth over time of deepest sink. ``sink_idx`` indexes
    # the filtered ``hip_ys`` list, so it must be paired with
    # ``filtered_frame_numbers`` rather than the unfiltered ``kp_seq``.
    sink_idx = hip_ys.index(max_y)
    if sink_idx > 0:
        frames_to_sink = filtered_frame_numbers[sink_idx] - filtered_frame_numbers[0]
        time_to_sink = frames_to_sink / max(fps, 1.0)
        com_velocity = depth_yards / max(time_to_sink, 0.001)
    else:
        com_velocity = 0.0

    confidence = statistics.mean(confs)
    return {
        "metric_name": "pose_wr_hip_sink_depth",
        "metric_value": {
            "vertical_drop_yards": round(depth_yards, 3),
            "com_drop_velocity_yps": round(com_velocity, 3),
            "confidence": round(confidence, 3),
        },
        "unit": "yd",
        "confidence": round(confidence, 3),
        "tracklet_id": tracklet_id,
    }


def _wr_release_technique(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Classify WR release technique from arm/shoulder motion in first 0.4 s.

    Simplified heuristics based on wrist-relative-to-shoulder motion:
      - Club Rip  : wrist sweeps away from defender (outward arc)
      - Club Swim : wrist sweeps inward then out
      - Speed     : minimal arm engagement, pure acceleration
      - Gangster  : low, wide arm angle
      - Unknown   : insufficient keypoint confidence
    """
    if not kp_seq:
        return None

    wrist_xs: list[float] = []
    wrist_ys: list[float] = []
    confs: list[float] = []
    for entry in kp_seq:
        lw = kp(entry["keypoints"], "left_wrist")
        rw = kp(entry["keypoints"], "right_wrist")
        ls = kp(entry["keypoints"], "left_shoulder")
        rs = kp(entry["keypoints"], "right_shoulder")
        min_conf = min(lw["confidence"], rw["confidence"], ls["confidence"], rs["confidence"])
        if min_conf < 0.25:
            continue
        # Use the active (leading) wrist (the one further from centre)
        shoulder_cx = (ls["x"] + rs["x"]) / 2.0
        if abs(lw["x"] - shoulder_cx) > abs(rw["x"] - shoulder_cx):
            wrist_xs.append(lw["x"] - shoulder_cx)
            wrist_ys.append(lw["y"])
        else:
            wrist_xs.append(rw["x"] - shoulder_cx)
            wrist_ys.append(rw["y"])
        confs.append(min_conf)

    if len(wrist_xs) < 3:
        return {
            "metric_name": "pose_wr_release_technique",
            "metric_value": {
                "technique": "unknown",
                "confidence": 0.0,
            },
            "unit": "category",
            "confidence": 0.0,
            "tracklet_id": tracklet_id,
        }

    dx_total = wrist_xs[-1] - wrist_xs[0]
    dy_total = wrist_ys[-1] - wrist_ys[0]
    # Direction reversals in x
    reversals = sum(
        1
        for i in range(1, len(wrist_xs) - 1)
        if (wrist_xs[i] - wrist_xs[i - 1]) * (wrist_xs[i + 1] - wrist_xs[i]) < 0
    )

    avg_conf = statistics.mean(confs)
    if abs(dx_total) < 5 and abs(dy_total) < 10:
        technique = "speed"
    elif reversals >= 2:
        technique = "club_swim"
    elif dy_total > 15 and abs(dx_total) > 20:
        technique = "gangster"
    elif dx_total > 15:
        technique = "club_rip"
    else:
        technique = "unknown"

    return {
        "metric_name": "pose_wr_release_technique",
        "metric_value": {
            "technique": technique,
            "confidence": round(avg_conf, 3),
        },
        "unit": "category",
        "confidence": round(avg_conf, 3),
        "tracklet_id": tracklet_id,
    }


# ── QB Mechanics ──────────────────────────────────────────────────────────────


def _compute_qb_mechanics(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    snap_frame: int,
    throw_frame: int | None,
    fps: float,
    tracklet_id: str | None,
) -> list[dict[str, Any]]:
    """Compute all QB biomechanics metrics."""
    metrics: list[dict[str, Any]] = []

    dropback_seq = [e for e in kp_seq if e["frame_number"] >= snap_frame]
    if not dropback_seq:
        return metrics

    stride = _qb_stride_consistency(dropback_seq, fps, tracklet_id)
    if stride:
        metrics.append(stride)

    sep = _qb_shoulder_hip_separation(dropback_seq, fps, tracklet_id)
    if sep:
        metrics.append(sep)

    wt = _qb_weight_transfer(dropback_seq, tracklet_id)
    if wt:
        metrics.append(wt)

    if throw_frame is not None:
        rc = _qb_release_consistency(kp_seq, throw_frame, tracklet_id)
        if rc:
            metrics.append(rc)

    return metrics


def _qb_stride_consistency(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    fps: float,
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Compute stride length mean and std + knee flexion consistency during dropback."""
    stride_lengths: list[float] = []
    knee_flexions: list[float] = []
    confs: list[float] = []

    for entry in kp_seq:
        keypoints = entry["keypoints"]
        la = kp(keypoints, "left_ankle")
        ra = kp(keypoints, "right_ankle")
        lk = kp(keypoints, "left_knee")
        rk = kp(keypoints, "right_knee")
        lh = kp(keypoints, "left_hip")
        rh = kp(keypoints, "right_hip")

        min_conf = min(la["confidence"], ra["confidence"],
                       lk["confidence"], rk["confidence"])
        if min_conf < 0.3:
            continue

        stride_px = math.sqrt((la["x"] - ra["x"]) ** 2 + (la["y"] - ra["y"]) ** 2)
        stride_yd = stride_px * (13.3 / 1280.0)
        stride_lengths.append(stride_yd)

        lf = angle_degrees(
            (lh["x"], lh["y"]), (lk["x"], lk["y"]), (la["x"], la["y"])
        )
        rf = angle_degrees(
            (rh["x"], rh["y"]), (rk["x"], rk["y"]), (ra["x"], ra["y"])
        )
        knee_flexions.append((lf + rf) / 2.0)
        confs.append(min_conf)

    if len(stride_lengths) < 2:
        return None

    mean_stride = statistics.mean(stride_lengths)
    std_stride = statistics.stdev(stride_lengths) if len(stride_lengths) >= 2 else 0.0
    mean_knee = statistics.mean(knee_flexions)
    confidence = statistics.mean(confs)

    return {
        "metric_name": "pose_qb_stride_consistency",
        "metric_value": {
            "stride_length_mean_yards": round(mean_stride, 3),
            "stride_length_std": round(std_stride, 3),
            "knee_flexion_mean_degrees": round(mean_knee, 1),
            "sample_frames": len(stride_lengths),
            "confidence": round(confidence, 3),
        },
        "unit": "yd",
        "confidence": round(confidence, 3),
        "tracklet_id": tracklet_id,
    }


def _qb_shoulder_hip_separation(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    fps: float,
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Compute shoulder-hip separation (trunk rotation) and velocity."""
    sep_angles: list[float] = []
    frame_nums: list[int] = []
    confs: list[float] = []

    for entry in kp_seq:
        keypoints = entry["keypoints"]
        ls = kp(keypoints, "left_shoulder")
        rs = kp(keypoints, "right_shoulder")
        lh = kp(keypoints, "left_hip")
        rh = kp(keypoints, "right_hip")

        min_conf = min(ls["confidence"], rs["confidence"],
                       lh["confidence"], rh["confidence"])
        if min_conf < 0.3:
            continue

        shoulder_vec = (rs["x"] - ls["x"], rs["y"] - ls["y"])
        sep = angle_degrees(
            (ls["x"] + shoulder_vec[0], ls["y"] + shoulder_vec[1]),
            ((ls["x"] + rs["x"]) / 2, (ls["y"] + rs["y"]) / 2),
            ((lh["x"] + rh["x"]) / 2, (lh["y"] + rh["y"]) / 2),
        )
        sep_angles.append(sep)
        frame_nums.append(entry["frame_number"])
        confs.append(min_conf)

    if len(sep_angles) < 2:
        return None

    max_sep = max(sep_angles)
    # Rotation velocity: max angular change per second
    if len(sep_angles) >= 2 and len(frame_nums) >= 2:
        delta_angle = abs(sep_angles[-1] - sep_angles[0])
        delta_frames = max(frame_nums[-1] - frame_nums[0], 1)
        velocity_rad_per_s = math.radians(delta_angle) / (delta_frames / max(fps, 1.0))
    else:
        velocity_rad_per_s = 0.0

    confidence = statistics.mean(confs)

    return {
        "metric_name": "pose_qb_shoulder_hip_separation",
        "metric_value": {
            "separation_degrees": round(max_sep, 1),
            "trunk_rotation_velocity_radps": round(velocity_rad_per_s, 3),
            "confidence": round(confidence, 3),
        },
        "unit": "degrees",
        "confidence": round(confidence, 3),
        "tracklet_id": tracklet_id,
    }


def _qb_weight_transfer(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Score back-to-front weight transfer during dropback.

    Uses the relative ankle x-positions: back foot should decrease in
    dominance as weight shifts forward at release.  Score 0=no transfer, 1=full.
    """
    if len(kp_seq) < 3:
        return None

    first_entry = kp_seq[0]["keypoints"]
    last_entry = kp_seq[-1]["keypoints"]

    la_first = kp(first_entry, "left_ankle")
    ra_first = kp(first_entry, "right_ankle")
    la_last = kp(last_entry, "left_ankle")
    ra_last = kp(last_entry, "right_ankle")

    min_conf = min(la_first["confidence"], ra_first["confidence"],
                   la_last["confidence"], ra_last["confidence"])
    if min_conf < 0.3:
        return None

    # CoM x shift (positive = forward, i.e. towards LOS)
    com_first = (la_first["x"] + ra_first["x"]) / 2.0
    com_last = (la_last["x"] + ra_last["x"]) / 2.0
    shift = com_last - com_first

    # Normalise against frame width
    shift_score = min(max(shift / 80.0, 0.0), 1.0)

    return {
        "metric_name": "pose_qb_weight_transfer",
        "metric_value": {
            "shift_score": round(shift_score, 3),
            "com_x_shift_px": round(shift, 1),
            "confidence": round(min_conf, 3),
        },
        "unit": "score",
        "confidence": round(min_conf, 3),
        "tracklet_id": tracklet_id,
    }


def _qb_release_consistency(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    throw_frame: int,
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Measure arm-angle and release-height consistency around the throw frame.

    For a single throw we compute the instantaneous release angle.
    Over multiple sessions/reps these values are aggregated externally.
    """
    # Find frames within ±5 frames of throw
    window = [
        e for e in kp_seq
        if abs(e["frame_number"] - throw_frame) <= 5
    ]
    if not window:
        return None

    arm_angles: list[float] = []
    release_heights: list[float] = []
    confs: list[float] = []

    for entry in window:
        keypoints = entry["keypoints"]
        rs = kp(keypoints, "right_shoulder")
        re = kp(keypoints, "right_elbow")
        rw = kp(keypoints, "right_wrist")
        min_conf = min(rs["confidence"], re["confidence"], rw["confidence"])
        if min_conf < 0.25:
            continue
        arm_angle = angle_degrees(
            (rs["x"], rs["y"]),
            (re["x"], re["y"]),
            (rw["x"], rw["y"]),
        )
        arm_angles.append(arm_angle)
        release_heights.append(rw["y"])
        confs.append(min_conf)

    if not arm_angles:
        return None

    arm_std = statistics.stdev(arm_angles) if len(arm_angles) > 1 else 0.0
    height_std = statistics.stdev(release_heights) if len(release_heights) > 1 else 0.0
    confidence = statistics.mean(confs)

    return {
        "metric_name": "pose_qb_release_consistency",
        "metric_value": {
            "arm_angle_mean_degrees": round(statistics.mean(arm_angles), 1),
            "arm_angle_std_degrees": round(arm_std, 3),
            "release_height_mean_px": round(statistics.mean(release_heights), 1),
            "release_height_std_px": round(height_std, 3),
            "confidence": round(confidence, 3),
        },
        "unit": "degrees",
        "confidence": round(confidence, 3),
        "tracklet_id": tracklet_id,
    }


# ── All Players — Stride Symmetry & Drift ────────────────────────────────────


def _compute_stride_symmetry(
    kp_seq: list[dict[str, list[dict[str, Any]]]],
    fps: float,
    tracklet_id: str | None,
) -> dict[str, Any] | None:
    """Compute left/right stride symmetry.

    Left/right stride lengths derived from consecutive ankle-contact events.
    Asymmetry score > 0.15 → injury_risk_flag=True (feeds UT Athletics model).
    """
    gait = compute_gait_asymmetry(kp_seq)
    if gait is None:
        return None

    injury_risk = gait["asymmetry_score"] > _ASYMMETRY_RISK_THRESHOLD

    return {
        "metric_name": "pose_stride_symmetry",
        "metric_value": {
            "left_right_ratio": gait["left_right_ratio"],
            "asymmetry_score": gait["asymmetry_score"],
            "asymmetry_index": gait["asymmetry_index"],
            "injury_risk_flag": injury_risk,
            "confidence": gait["confidence"],
        },
        "unit": "ratio",
        "confidence": gait["confidence"],
        "tracklet_id": tracklet_id,
        "asymmetry_index": gait["asymmetry_index"],
        "gait_summary": gait,
    }


def _compute_biomechanical_drift(
    tracklets: list[dict[str, Any]],
    frame_keypoints: dict[int, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Detect biomechanical drift across OL tracklets in a clip.

    Compares mean torso angles in the first-third vs last-third of the
    on-screen window for the supplied tracklets.  ``frame_keypoints`` is the
    clip-wide per-frame keypoints map; we restrict the analysis to frames
    that fall inside at least one of ``tracklets``' ``[start_frame,
    end_frame]`` ranges so frames where no relevant player is on screen do
    not dilute the drift signal.

    Declining = later reps have higher torso angles (fatigue / sloppy technique).
    """
    if not frame_keypoints:
        return None

    # Build the union of frame ranges covered by the supplied tracklets and
    # only consider frames inside at least one range.  Tracklets without a
    # well-formed ``[start_frame, end_frame]`` are dropped.
    ranges = [
        (int(t.get("start_frame", 0)), int(t.get("end_frame", 0)))
        for t in tracklets
    ]
    ranges = [(s, e) for s, e in ranges if e >= s]
    if not ranges:
        return None

    frames = sorted(
        fn for fn in frame_keypoints if any(s <= fn <= e for s, e in ranges)
    )
    n = len(frames)
    if n < 6:
        return None

    first_third = frames[: n // 3]
    last_third = frames[2 * n // 3 :]

    def _mean_torso_angle(frame_list: list[int]) -> float:
        angles: list[float] = []
        for fn in frame_list:
            keypoints = frame_keypoints.get(fn, [])
            if not keypoints:
                continue
            lh = kp(keypoints, "left_hip")
            rh = kp(keypoints, "right_hip")
            ls = kp(keypoints, "left_shoulder")
            rs = kp(keypoints, "right_shoulder")
            if min(lh["confidence"], rh["confidence"],
                   ls["confidence"], rs["confidence"]) < 0.3:
                continue
            hip_c = midpoint((lh["x"], lh["y"]), (rh["x"], rh["y"]))
            shoulder_c = midpoint(
                (ls["x"], ls["y"]), (rs["x"], rs["y"])
            )
            angles.append(vector_angle_from_vertical(hip_c, shoulder_c))
        return statistics.mean(angles) if angles else 0.0

    early_angle = _mean_torso_angle(first_third)
    late_angle = _mean_torso_angle(last_third)

    if early_angle < 1e-3 and late_angle < 1e-3:
        return None

    delta = late_angle - early_angle
    if delta > 5.0:
        trend = "declining"
    elif delta < -5.0:
        trend = "improving"
    else:
        trend = "stable"

    return {
        "metric_name": "pose_biomechanical_drift",
        "metric_value": {
            "consistency_trend": trend,
            "early_mean_torso_degrees": round(early_angle, 1),
            "late_mean_torso_degrees": round(late_angle, 1),
            "delta_degrees": round(delta, 1),
            "window_frames": n,
            "confidence": 0.55,
        },
        "unit": "category",
        "confidence": 0.55,
        "tracklet_id": None,
    }


# ── Private helpers ────────────────────────────────────────────────────────────


def _build_keypoint_sequence(
    frame_keypoints: dict[int, list[dict[str, Any]]],
    start_frame: int,
    end_frame: int,
) -> list[dict[str, Any]]:
    """Build a sorted list of {frame_number, keypoints} dicts for a tracklet range."""
    seq = []
    for fn in sorted(frame_keypoints.keys()):
        if start_frame <= fn <= end_frame:
            seq.append({"frame_number": fn, "keypoints": frame_keypoints[fn]})
    return seq


def _event_frame(events: list[dict[str, Any]], event_type: str) -> int | None:
    evt = next((e for e in events if e.get("event_type") == event_type), None)
    if evt:
        return evt.get("frame_number")
    return None


def _write_keypoints(
    clip_id: str,
    tracklet_id: str | None,
    kp_seq: list[dict[str, Any]],
    job_id: str,
) -> list[str]:
    """Write raw keypoints to the backend pose_keypoints table."""
    ids: list[str] = []
    for entry in kp_seq:
        try:
            head_orientation = compute_head_yaw(entry["keypoints"])
            resp = backend.create_pose_keypoints(
                tracklet_id=tracklet_id,
                frame_number=entry["frame_number"],
                keypoints=entry["keypoints"],
                head_yaw_degrees=(
                    head_orientation.get("head_yaw_degrees")
                    if head_orientation is not None
                    else None
                ),
                head_orientation_confidence=(
                    head_orientation.get("confidence")
                    if head_orientation is not None
                    else None
                ),
                job_id=job_id,
            )
            ids.append(resp.get("id", ""))
        except Exception as exc:
            log.debug(
                "keypoint_write_failed",
                frame=entry["frame_number"],
                error=str(exc),
            )
    return ids


def _compute_body_orientation_proxy(
    kp_seq: list[dict[str, Any]],
    tracklet_id: str | None,
    *,
    player_id: str | None,
    position_group: str | None,
) -> dict[str, Any] | None:
    """Create the tracklet-level body-orientation proxy metric."""
    summary = summarize_body_orientation(kp_seq)
    if summary is None:
        return None

    reason_codes = list(summary.get("reason_codes", []))
    if player_id is None:
        reason_codes.append("anonymous_track")

    metric_value = {
        **summary,
        "track_id": tracklet_id,
        "player_id": player_id,
        "position_group": position_group,
        "proxy_kind": "body_orientation_proxy",
        "reason_codes": sorted(set(reason_codes)),
    }
    return {
        "tracklet_id": tracklet_id,
        "metric_name": "pose_body_orientation_proxy",
        "metric_value": metric_value,
        "unit": "degrees",
        "confidence": summary.get("confidence"),
    }
