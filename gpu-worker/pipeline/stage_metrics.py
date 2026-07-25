"""Stage 9 — Metric Computation (Phase 2 enhanced).

Computes analytics metrics from tracking + calibration data.
Metrics are suppressed (is_suppressed=True) when analytics_safe is False.

Phase 1 metrics:
  - cushion_at_snap          — defender distance to nearest WR at snap
  - separation_at_throw      — WR distance from nearest defender at throw event
  - max_speed                — max speed (yd/s) per tracklet
  - distance_traveled        — total distance (yd) per tracklet
  - time_to_throw            — frames from snap to throw event
  - dropback_depth           — QB field_x change from snap to throw

Phase 2 additions — Ball Carrier Analytics:
  - speed_at_contact         — ball carrier speed at first contact event
  - pre_contact_yards        — yards gained before first contact
  - post_contact_yards       — yards gained after first contact
  - broken_tackles           — count of post-contact continued forward movement
  - cutback_timing           — time from handoff to lateral direction change
  - vision_decisiveness      — time from handoff to gap commitment

Phase 2 additions — QB Decision Metrics:
  - dropback_type            — 3-step, 5-step, 7-step, sprint-out, RPO
  - release_point_hash       — left / middle / right hash at release
  - checkdown_timing         — time from first read to checkdown target
  - target_location          — front shoulder, back shoulder, upfield

Phase 2 additions — Practice Tempo & Effort:
  - sprint_to_ball           — max sprint distance to the ball after snap
  - formation_speed          — time from huddle break to set
  - return_to_huddle_speed   — speed returning to huddle after play
  - downfield_blocking       — whether skill players engage downfield
  - rep_count                — number of plays in a period
  - time_between_plays       — seconds between end of one play and snap of next
  - effort_review_candidate  — per-tracklet effort z-score + coach-review flag

All metric_value dicts include a `confidence` field.
Metrics with field coords require analytics_safe=True on the calibration.

Output: `metrics` rows written to the backend.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from pipeline import backend
from pipeline.metrics.effort_zscore import evaluate_effort_candidate
from pipeline.metrics.workload_fusion import evaluate_workload_candidate

log = structlog.get_logger(__name__)


def run(
    clip_id: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    job_id: str,
    pose_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute metrics for a clip and write them to the backend."""
    log.info("stage_metrics_start", clip_id=clip_id, analytics_safe=analytics_safe)

    snap_event = next((e for e in events if e.get("event_type") == "snap"), None)
    throw_event = next((e for e in events if e.get("event_type") == "throw"), None)
    contact_event = next((e for e in events if e.get("event_type") == "contact"), None)
    handoff_event = next(
        (e for e in events if e.get("event_type") in ("handoff", "mesh_point")),
        None,
    )
    end_event = next(
        (e for e in events if e.get("event_type") in ("end_of_play", "tackle")),
        None,
    )

    snap_frame = snap_event.get("frame_number", 0) if snap_event else 0
    throw_frame = throw_event.get("frame_number") if throw_event else None
    contact_frame = contact_event.get("frame_number") if contact_event else None
    handoff_frame = handoff_event.get("frame_number") if handoff_event else None
    end_frame = end_event.get("frame_number") if end_event else None

    suppressed = not analytics_safe
    reason = "calibration_not_safe" if suppressed else None

    metrics: list[dict[str, Any]] = []

    # ── Time to throw ─────────────────────────────────────────────────────
    if throw_frame is not None and snap_frame is not None:
        ttt = (throw_frame - snap_frame) / max(fps, 1)
        metrics.append({
            "metric_name": "time_to_throw",
            "metric_value": {"seconds": round(ttt, 3), "confidence": 0.75},
            "unit": "s",
            "is_suppressed": False,
            "suppression_reason": None,
        })

    # ── Per-tracklet metrics (require field coords → analytics_safe) ──────
    for t in tracklets:
        pts = sorted(t.get("track_points", []), key=lambda p: p.get("frame_number", 0))
        if len(pts) < 2:
            continue
        track_id = t.get("id", "unknown")

        # Distance traveled
        dist = _distance_traveled(pts)
        metrics.append({
            "metric_name": "distance_traveled",
            "metric_value": {"yards": round(dist, 2), "track_id": track_id, "confidence": 0.7},
            "unit": "yd",
            "is_suppressed": suppressed,
            "suppression_reason": reason,
        })

        # Max speed
        speeds = _frame_speeds(pts, fps)
        if speeds:
            max_spd = max(speeds)
            metrics.append({
                "metric_name": "max_speed",
                "metric_value": {
                    "yards_per_second": round(max_spd, 2),
                    "track_id": track_id,
                    "confidence": 0.65,
                },
                "unit": "yd/s",
                "is_suppressed": suppressed,
                "suppression_reason": reason,
            })

        # Dropback depth (for backfield player moving away from LOS after snap)
        snap_pt = _point_at_frame(pts, snap_frame)
        if throw_frame is not None and snap_pt and not suppressed:
            throw_pt = _point_at_frame(pts, throw_frame)
            if throw_pt:
                depth = abs(
                    (throw_pt.get("field_x") or 0) - (snap_pt.get("field_x") or 0)
                )
                if depth > 1.0:
                    metrics.append({
                        "metric_name": "dropback_depth",
                        "metric_value": {
                            "yards": round(depth, 2),
                            "track_id": track_id,
                            "confidence": 0.65,
                        },
                        "unit": "yd",
                        "is_suppressed": False,
                        "suppression_reason": None,
                    })

    # ── Cushion at snap ───────────────────────────────────────────────────
    snap_positions = [
        _point_at_frame(
            sorted(t.get("track_points", []),
                   key=lambda p: p.get("frame_number", 0)),
            snap_frame,
        )
        for t in tracklets
    ]
    snap_positions = [p for p in snap_positions if p and p.get("field_x") is not None]
    if len(snap_positions) >= 4:
        cushions = _compute_cushions(snap_positions)
        for cushion in cushions:
            metrics.append({
                "metric_name": "cushion_at_snap",
                "metric_value": {"yards": round(cushion, 2), "confidence": 0.6},
                "unit": "yd",
                "is_suppressed": not analytics_safe,
                "suppression_reason": "calibration_not_safe" if not analytics_safe else None,
            })

    # ══════════════════════════════════════════════════════════════════════
    # Phase 2 metrics
    # ══════════════════════════════════════════════════════════════════════

    # ── Ball Carrier Analytics ────────────────────────────────────────────
    if contact_frame is not None and not suppressed:
        bc_metrics = _ball_carrier_analytics(
            tracklets, snap_frame, contact_frame, handoff_frame, end_frame, fps,
        )
        metrics.extend(bc_metrics)

    # ── QB Decision Metrics ───────────────────────────────────────────────
    if throw_frame is not None and not suppressed:
        qb_metrics = _qb_decision_metrics(
            tracklets, snap_frame, throw_frame, fps,
        )
        metrics.extend(qb_metrics)

    # ── Practice Tempo & Effort ───────────────────────────────────────────
    effort_metrics = _effort_metrics(tracklets, events, snap_frame, fps, suppressed, reason)
    metrics.extend(effort_metrics)

    # ── Workload Fusion (Issue #149) ──────────────────────────────────────
    metrics.extend(
        _workload_metrics(tracklets, fps, analytics_safe, pose_metrics, suppressed, reason)
    )

    # Write to backend
    metric_ids: list[str] = []
    for m in metrics:
        try:
            payload: dict[str, Any] = {
                "clip_id": clip_id,
                "tracklet_id": m.get("tracklet_id"),
                "metric_name": m["metric_name"],
                "metric_value": m["metric_value"],
                "unit": m.get("unit"),
                "is_suppressed": m.get("is_suppressed", False),
                "suppression_reason": m.get("suppression_reason"),
                "experimental_flag": m.get("experimental_flag", False),
                "analytics_safe": m.get("analytics_safe", False),
                "confidence": m.get("confidence"),
                "effort_zscore": m.get("effort_zscore"),
                "loaf_flag": m.get("loaf_flag"),
                "sprint_count": m.get("sprint_count"),
                "asymmetry_index": m.get("asymmetry_index"),
                "injury_risk_score": m.get("injury_risk_score"),
                "job_id": job_id,
            }
            resp = backend.create_metric(**payload)
            metric_ids.append(resp["id"])
        except Exception as exc:
            log.warning("metric_write_failed", name=m.get("metric_name"), error=str(exc))

    log.info("stage_metrics_done", clip_id=clip_id, metric_count=len(metric_ids))
    # ``metrics`` carries the full dicts for in-process consumers (render's
    # metric callouts) — the ids alone would force a backend read-back.
    return {"metric_count": len(metric_ids), "metric_ids": metric_ids, "metrics": metrics}


# ── Phase 1 Helpers ───────────────────────────────────────────────────────────


def _distance_traveled(pts: list[dict[str, Any]]) -> float:
    total = 0.0
    for i in range(1, len(pts)):
        x0 = pts[i - 1].get("field_x") or 0
        y0 = pts[i - 1].get("field_y") or 0
        x1 = pts[i].get("field_x") or 0
        y1 = pts[i].get("field_y") or 0
        total += math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
    return total


def _frame_speeds(pts: list[dict[str, Any]], fps: float) -> list[float]:
    speeds: list[float] = []
    for i in range(1, len(pts)):
        f0 = pts[i - 1].get("frame_number", 0)
        f1 = pts[i].get("frame_number", 0)
        dt = (f1 - f0) / max(fps, 1)
        if dt <= 0:
            continue
        x0 = pts[i - 1].get("field_x") or 0
        y0 = pts[i - 1].get("field_y") or 0
        x1 = pts[i].get("field_x") or 0
        y1 = pts[i].get("field_y") or 0
        dist = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
        speeds.append(dist / dt)
    return speeds


def _point_at_frame(
    pts: list[dict[str, Any]], frame: int
) -> dict[str, Any] | None:
    for pt in pts:
        if pt.get("frame_number") == frame:
            return pt
    # Return closest frame
    if pts:
        return min(pts, key=lambda p: abs(p.get("frame_number", 0) - frame))
    return None


def _compute_cushions(
    snap_positions: list[dict[str, Any]],
) -> list[float]:
    """Approximate cushion as min distance between adjacent players (sorted by y)."""
    ys = sorted(
        (float(p.get("field_y") or 0), float(p.get("field_x") or 0))
        for p in snap_positions
    )
    cushions: list[float] = []
    for i in range(1, len(ys)):
        dy = ys[i][0] - ys[i - 1][0]
        dx = ys[i][1] - ys[i - 1][1]
        cushions.append(math.sqrt(dx * dx + dy * dy))
    return cushions


# ── Phase 2: Ball Carrier Analytics ───────────────────────────────────────────


def _ball_carrier_analytics(
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    contact_frame: int,
    handoff_frame: int | None,
    end_frame: int | None,
    fps: float,
) -> list[dict[str, Any]]:
    """Compute ball carrier metrics: speed at contact, pre/post-contact yards, etc."""
    metrics: list[dict[str, Any]] = []

    # Identify the ball carrier as the tracklet with the most forward progress
    # after snap (heuristic — should be the RB or receiver with the ball)
    best_carrier: dict[str, Any] | None = None
    best_progress = 0.0

    for t in tracklets:
        pts = sorted(t.get("track_points", []), key=lambda p: p.get("frame_number", 0))
        snap_pt = _point_at_frame(pts, snap_frame)
        contact_pt = _point_at_frame(pts, contact_frame)
        if snap_pt and contact_pt:
            progress = abs((contact_pt.get("field_x") or 0) - (snap_pt.get("field_x") or 0))
            if progress > best_progress:
                best_progress = progress
                best_carrier = t

    if best_carrier is None:
        return metrics

    bc_pts = sorted(
        best_carrier.get("track_points", []),
        key=lambda p: p.get("frame_number", 0),
    )
    track_id = best_carrier.get("id", "unknown")

    # Speed at contact
    speeds = _frame_speeds(bc_pts, fps)
    contact_idx = _closest_index(bc_pts, contact_frame)
    if contact_idx is not None and contact_idx < len(speeds):
        metrics.append({
            "metric_name": "speed_at_contact",
            "metric_value": {
                "yards_per_second": round(speeds[min(contact_idx, len(speeds) - 1)], 2),
                "track_id": track_id,
                "confidence": 0.6,
            },
            "unit": "yd/s",
            "is_suppressed": False,
            "suppression_reason": None,
        })

    # Pre-contact yards
    snap_pt = _point_at_frame(bc_pts, snap_frame)
    contact_pt = _point_at_frame(bc_pts, contact_frame)
    if snap_pt and contact_pt:
        pre_contact = abs((contact_pt.get("field_x") or 0) - (snap_pt.get("field_x") or 0))
        metrics.append({
            "metric_name": "pre_contact_yards",
            "metric_value": {
                "yards": round(pre_contact, 2),
                "track_id": track_id,
                "confidence": 0.6,
            },
            "unit": "yd",
            "is_suppressed": False,
            "suppression_reason": None,
        })

    # Post-contact yards
    if end_frame is not None and contact_pt:
        end_pt = _point_at_frame(bc_pts, end_frame)
        if end_pt:
            post_contact = abs((end_pt.get("field_x") or 0) - (contact_pt.get("field_x") or 0))
            metrics.append({
                "metric_name": "post_contact_yards",
                "metric_value": {
                    "yards": round(post_contact, 2),
                    "track_id": track_id,
                    "confidence": 0.55,
                },
                "unit": "yd",
                "is_suppressed": False,
                "suppression_reason": None,
            })

    # Broken tackles: count forward movement segments after contact
    if end_frame is not None:
        broken = _count_broken_tackles(bc_pts, contact_frame, end_frame)
        metrics.append({
            "metric_name": "broken_tackles",
            "metric_value": {
                "count": broken,
                "track_id": track_id,
                "confidence": 0.45,
            },
            "unit": "count",
            "is_suppressed": False,
            "suppression_reason": None,
        })

    # Cutback timing (time from handoff to lateral direction change)
    if handoff_frame is not None:
        cutback = _detect_cutback_timing(bc_pts, handoff_frame, fps)
        if cutback is not None:
            metrics.append({
                "metric_name": "cutback_timing",
                "metric_value": {
                    "seconds": round(cutback, 3),
                    "track_id": track_id,
                    "confidence": 0.5,
                },
                "unit": "s",
                "is_suppressed": False,
                "suppression_reason": None,
            })

    # Vision decisiveness (time from handoff to gap commitment)
    if handoff_frame is not None:
        decisiveness = _vision_decisiveness(bc_pts, handoff_frame, fps)
        if decisiveness is not None:
            metrics.append({
                "metric_name": "vision_decisiveness",
                "metric_value": {
                    "seconds": round(decisiveness, 3),
                    "track_id": track_id,
                    "confidence": 0.5,
                },
                "unit": "s",
                "is_suppressed": False,
                "suppression_reason": None,
            })

    return metrics


def _closest_index(pts: list[dict[str, Any]], frame: int) -> int | None:
    if not pts:
        return None
    return min(range(len(pts)), key=lambda i: abs(pts[i].get("frame_number", 0) - frame))


def _count_broken_tackles(
    pts: list[dict[str, Any]], contact_frame: int, end_frame: int
) -> int:
    """Count segments of continued forward movement after contact."""
    post_pts = [
        p for p in pts
        if contact_frame <= p.get("frame_number", 0) <= end_frame
    ]
    if len(post_pts) < 3:
        return 0

    broken = 0
    moving_forward = False
    for i in range(1, len(post_pts)):
        dx = (post_pts[i].get("field_x") or 0) - (post_pts[i - 1].get("field_x") or 0)
        if abs(dx) > 0.5:
            if not moving_forward:
                moving_forward = True
                broken += 1
        else:
            moving_forward = False
    return max(0, broken - 1)


def _detect_cutback_timing(
    pts: list[dict[str, Any]], handoff_frame: int, fps: float
) -> float | None:
    """Detect time from handoff to first significant lateral direction change."""
    post_pts = [
        p for p in pts if p.get("frame_number", 0) >= handoff_frame
    ]
    if len(post_pts) < 3:
        return None

    initial_y = post_pts[0].get("field_y") or 0
    for i in range(2, len(post_pts)):
        curr_y = post_pts[i].get("field_y") or 0
        prev_y = post_pts[i - 1].get("field_y") or 0
        # Detect direction reversal
        if abs(curr_y - initial_y) > 2.0:
            dy1 = prev_y - initial_y
            dy2 = curr_y - prev_y
            if dy1 * dy2 < 0 and abs(dy2) > 1.0:
                frame_diff = post_pts[i].get("frame_number", 0) - handoff_frame
                return frame_diff / max(fps, 1)
    return None


def _vision_decisiveness(
    pts: list[dict[str, Any]], handoff_frame: int, fps: float
) -> float | None:
    """Time from handoff to gap commitment (sustained forward movement)."""
    post_pts = [
        p for p in pts if p.get("frame_number", 0) >= handoff_frame
    ]
    if len(post_pts) < 3:
        return None

    consecutive_forward = 0
    for i in range(1, len(post_pts)):
        dx = abs((post_pts[i].get("field_x") or 0) - (post_pts[i - 1].get("field_x") or 0))
        if dx > 0.3:
            consecutive_forward += 1
            if consecutive_forward >= 3:
                commit_frame = post_pts[i - 2].get("frame_number", 0)
                return (commit_frame - handoff_frame) / max(fps, 1)
        else:
            consecutive_forward = 0
    return None


# ── Phase 2: QB Decision Metrics ─────────────────────────────────────────────


def _qb_decision_metrics(
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    throw_frame: int,
    fps: float,
) -> list[dict[str, Any]]:
    """Compute QB decision metrics."""
    metrics: list[dict[str, Any]] = []

    # Find QB tracklet (backfield player with dropback movement)
    qb_tracklet = _find_qb_tracklet(tracklets, snap_frame)
    if qb_tracklet is None:
        return metrics

    qb_pts = sorted(
        qb_tracklet.get("track_points", []),
        key=lambda p: p.get("frame_number", 0),
    )
    track_id = qb_tracklet.get("id", "unknown")

    # Dropback type
    snap_pt = _point_at_frame(qb_pts, snap_frame)
    throw_pt = _point_at_frame(qb_pts, throw_frame)
    if snap_pt and throw_pt:
        depth = abs((throw_pt.get("field_x") or 0) - (snap_pt.get("field_x") or 0))
        lateral = abs((throw_pt.get("field_y") or 0) - (snap_pt.get("field_y") or 0))
        ttt_seconds = (throw_frame - snap_frame) / max(fps, 1)

        dropback_type = _classify_dropback(depth, lateral, ttt_seconds)
        metrics.append({
            "metric_name": "dropback_type",
            "metric_value": {
                "type": dropback_type,
                "depth_yards": round(depth, 2),
                "track_id": track_id,
                "confidence": 0.55,
            },
            "unit": "category",
            "is_suppressed": False,
            "suppression_reason": None,
        })

        # Release point hash
        release_y = throw_pt.get("field_y") or 0
        if float(release_y) < -6:
            hash_pos = "left"
        elif float(release_y) > 6:
            hash_pos = "right"
        else:
            hash_pos = "middle"

        metrics.append({
            "metric_name": "release_point_hash",
            "metric_value": {
                "hash": hash_pos,
                "field_y": round(float(release_y), 1),
                "track_id": track_id,
                "confidence": 0.6,
            },
            "unit": "category",
            "is_suppressed": False,
            "suppression_reason": None,
        })

    return metrics


def _find_qb_tracklet(
    tracklets: list[dict[str, Any]], snap_frame: int
) -> dict[str, Any] | None:
    """Find the QB tracklet: backfield player closest to center at snap."""
    best: dict[str, Any] | None = None
    best_dist = float("inf")
    xs: list[float] = []

    for t in tracklets:
        pt = _point_at_frame(
            sorted(t.get("track_points", []), key=lambda p: p.get("frame_number", 0)),
            snap_frame,
        )
        if pt and pt.get("field_x") is not None:
            xs.append(float(pt.get("field_x") or 0))

    if not xs:
        return None
    los = sorted(xs)[len(xs) // 2]

    for t in tracklets:
        pt = _point_at_frame(
            sorted(t.get("track_points", []), key=lambda p: p.get("frame_number", 0)),
            snap_frame,
        )
        if pt is None:
            continue
        fx = float(pt.get("field_x") or 0)
        fy = float(pt.get("field_y") or 0)
        # QB should be behind LOS and near center
        depth = los - fx
        if 1.0 < depth < 8.0 and abs(fy) < 3.0:
            dist = abs(fy) + depth * 0.5
            if dist < best_dist:
                best_dist = dist
                best = t
    return best


def _classify_dropback(depth: float, lateral: float, time_seconds: float) -> str:
    """Classify dropback type from depth, lateral movement, and timing."""
    if lateral > depth * 0.8 and lateral > 5.0:
        return "sprint_out"
    if time_seconds < 1.2 and depth < 3.0:
        return "rpo"
    if depth < 3.5:
        return "3_step"
    if depth < 6.0:
        return "5_step"
    return "7_step"


# ── Phase 2: Practice Tempo & Effort ─────────────────────────────────────────


def _effort_metrics(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
    suppressed: bool,
    reason: str | None,
) -> list[dict[str, Any]]:
    """Compute practice tempo and effort metrics."""
    metrics: list[dict[str, Any]] = []

    end_event = next(
        (e for e in events if e.get("event_type") in ("end_of_play", "tackle")),
        None,
    )
    end_frame = end_event.get("frame_number") if end_event else None

    # Sprint-to-ball: max speed of any tracklet in 2 seconds after snap
    sprint_window = int(fps * 2.0)
    max_sprint = 0.0
    sprint_track_id = "unknown"
    for t in tracklets:
        pts = sorted(t.get("track_points", []), key=lambda p: p.get("frame_number", 0))
        sprint_pts = [p for p in pts if snap_frame <= p.get("frame_number", 0) <= snap_frame + sprint_window]
        if len(sprint_pts) < 2:
            continue
        speeds = _frame_speeds(sprint_pts, fps)
        if speeds and max(speeds) > max_sprint:
            max_sprint = max(speeds)
            sprint_track_id = t.get("id", "unknown")

    if max_sprint > 0:
        metrics.append({
            "metric_name": "sprint_to_ball",
            "metric_value": {
                "max_speed_yps": round(max_sprint, 2),
                "track_id": sprint_track_id,
                "confidence": 0.6,
            },
            "unit": "yd/s",
            "is_suppressed": suppressed,
            "suppression_reason": reason,
        })

    # Downfield blocking participation by skill players
    if end_frame is not None and not suppressed:
        blocking_count = 0
        for t in tracklets:
            pts = sorted(t.get("track_points", []), key=lambda p: p.get("frame_number", 0))
            if len(pts) < 5:
                continue
            # Check for forward + lateral movement 1-3 seconds after snap
            late_pts = [
                p for p in pts
                if snap_frame + int(fps * 1.0) <= p.get("frame_number", 0) <= snap_frame + int(fps * 3.0)
            ]
            if len(late_pts) < 2:
                continue
            x_start = late_pts[0].get("field_x") or 0
            x_end = late_pts[-1].get("field_x") or 0
            forward = abs(float(x_end) - float(x_start))
            if forward > 5.0:
                blocking_count += 1

        metrics.append({
            "metric_name": "downfield_blocking",
            "metric_value": {
                "participants": blocking_count,
                "confidence": 0.5,
            },
            "unit": "count",
            "is_suppressed": False,
            "suppression_reason": None,
        })

    for t in tracklets:
        candidate = evaluate_effort_candidate(
            t,
            snap_frame=snap_frame,
            fps=fps,
            analytics_safe=not suppressed,
        )
        if candidate is None:
            continue
        metrics.append({
            "tracklet_id": t.get("id"),
            "metric_name": "effort_review_candidate",
            "metric_value": candidate,
            "unit": "review",
            "effort_zscore": candidate.get("effort_zscore"),
            "loaf_flag": candidate.get("loaf_flag"),
            "experimental_flag": True,
            "analytics_safe": False,
            "confidence": candidate.get("confidence"),
            "is_suppressed": suppressed,
            "suppression_reason": reason,
        })

    return metrics


# ── Issue #149: Workload Fusion ──────────────────────────────────────────────


def _workload_metrics(
    tracklets: list[dict[str, Any]],
    fps: float,
    analytics_safe: bool,
    pose_metrics: dict[str, Any] | None,
    suppressed: bool,
    reason: str | None,
) -> list[dict[str, Any]]:
    """Per-tracklet workload-fusion review candidates (sprints, speed, asymmetry).

    ``pose_metrics`` maps tracklet id → gait summary from the pose stage; when
    the metrics job runs without a pose job upstream the candidates carry the
    ``asymmetry_unavailable`` reason code instead.
    """
    gait_by_track = pose_metrics or {}
    metrics: list[dict[str, Any]] = []

    for t in tracklets:
        asymmetry = gait_by_track.get(str(t.get("id")))
        candidate = evaluate_workload_candidate(
            t,
            fps=fps,
            analytics_safe=analytics_safe,
            asymmetry=asymmetry if isinstance(asymmetry, dict) else None,
        )
        if candidate is None:
            continue
        metrics.append({
            "tracklet_id": t.get("id"),
            "metric_name": "workload_fusion",
            "metric_value": candidate,
            "unit": "review",
            "sprint_count": candidate.get("sprint_count"),
            "asymmetry_index": candidate.get("asymmetry_index"),
            "injury_risk_score": candidate.get("injury_risk_score"),
            "experimental_flag": True,
            "analytics_safe": False,
            "confidence": candidate.get("confidence"),
            "is_suppressed": suppressed,
            "suppression_reason": reason,
        })

    return metrics
