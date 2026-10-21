"""OL observations and experimental tracking hypotheses.

Original named gaps and LOS clear lanes require explicit role/LOS evidence,
measured field-coordinate body extents, and complete defender occupancy.
Center-to-center spacing is a separate observation. Legacy double-team,
stunt, shed and pursuit heuristics remain unvalidated experimental evidence;
they do not establish interaction, assignment, recognition, or block success.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from pipeline import backend
from pipeline.observations import field_position_at
from pipeline.runtime import PipelineInterrupted, check_control
from pipeline.video_timeline import VideoTimeline

log = structlog.get_logger(__name__)

OL_LOS_RANGE_YD = 2.0
DL_LOS_RANGE_YD = 3.0
DOUBLE_TEAM_PROXIMITY_YD = 2.5
STUNT_LATERAL_THRESHOLD_YD = 3.0


def run(
    clip_id: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    job_id: str,
    *,
    timeline: VideoTimeline | None = None,
    attacking_direction: int | None = None,
) -> dict[str, Any]:
    """Compute OL performance metrics and write to backend."""
    log.info("stage_oline_start", clip_id=clip_id)

    snap_event = next((e for e in events if e.get("event_type") == "snap"), None)
    if snap_event is None:
        return {
            "metric_count": 0,
            "metric_ids": [],
            "metrics": [],
            "skipped": True,
            "reason": "snap_not_observed",
        }
    snap_frame = snap_event.get("frame_number", 0) if snap_event else 0

    throw_event = next(
        (
            e
            for e in events
            if e.get("event_type")
            in {
                "throw",
                "pass_release",
                "forward_pass_release",
            }
        ),
        None,
    )
    throw_frame = throw_event.get("frame_number") if throw_event else None

    mesh_event = next(
        (e for e in events if e.get("event_type") in ("handoff", "mesh_point")),
        None,
    )
    mesh_frame = mesh_event.get("frame_number") if mesh_event else None

    from pipeline.position_evidence import named_gaps, original_front

    front = original_front(tracklets, snap_event, timeline, attacking_direction)
    los_x = front["los_x"] if front else _estimate_los(tracklets, snap_frame, fps)
    if los_x is None:
        return {
            "metric_count": 0,
            "metric_ids": [],
            "metrics": [],
            "skipped": True,
            "reason": "line_of_scrimmage_unresolved",
        }

    ol_tracklets = (
        [t for t in tracklets if str(t.get("id")) in set(front["roles"].values())]
        if front
        else _identify_ol(tracklets, snap_frame, los_x, fps)
    )
    dl_tracklets = _identify_dl(tracklets, snap_frame, los_x, fps)

    suppressed = not analytics_safe
    reason = "calibration_not_safe" if suppressed else None

    from pipeline.oline_observations import measured_lanes

    metrics: list[dict[str, Any]] = measured_lanes(tracklets, events, timeline, attacking_direction)
    from pipeline.lane_windows import lane_windows

    metrics.extend(lane_windows(tracklets, events, timeline, attacking_direction, lanes=list(metrics)))
    gaps = named_gaps(front) if front else []

    # ── Gap widths ────────────────────────────────────────────────────────
    target_frame = mesh_frame if mesh_frame else snap_frame + int(fps * 0.5)
    for frame, name in ((snap_frame, "pre_snap_split"), (target_frame, "geometric_spacing")):
        for measurement in _gap_measurements(ol_tracklets, snap_frame, frame, fps):
            named = next(
                (
                    gap
                    for gap in gaps
                    if set(gap["initial_pair"]) == set(measurement["initial_pair"])
                ),
                None,
            )
            if named:
                measurement.update(gap_label=named["gap_label"], subject_id=named["subject_id"])
            if name == "geometric_spacing":
                by_id = {t.get("id"): t for t in ol_tracklets}
                left_id, right_id = measurement["initial_pair"]
                measurement["series"] = _spacing_series(
                    by_id[left_id], by_id[right_id], snap_frame, fps
                )
            metrics.append(
                {
                    "metric_name": name,
                    "metric_value": measurement,
                    "unit": "yd",
                    "is_suppressed": suppressed,
                    "suppression_reason": reason,
                }
            )

    # ── Double-team detection ─────────────────────────────────────────────
    double_teams = _detect_double_teams(ol_tracklets, dl_tracklets, snap_frame, fps)
    for dt in double_teams:
        metrics.append(
            {
                "metric_name": "double_team",
                "metric_value": dt,
                "unit": "frames",
                "is_suppressed": suppressed,
                "suppression_reason": reason,
            }
        )

    # ── Stunt / twist recognition ─────────────────────────────────────────
    stunts = _detect_stunts(dl_tracklets, snap_frame, fps)
    for stunt in stunts:
        metrics.append(
            {
                "metric_name": "stunt_twist",
                "metric_value": stunt,
                "unit": "yd",
                "is_suppressed": suppressed,
                "suppression_reason": reason,
            }
        )

    # ── Block shed timing ─────────────────────────────────────────────────
    shed_times = _compute_block_shed_timing(ol_tracklets, dl_tracklets, snap_frame, fps)
    for shed in shed_times:
        metrics.append(
            {
                "metric_name": "block_shed_time",
                "metric_value": shed,
                "unit": "s",
                "is_suppressed": suppressed,
                "suppression_reason": reason,
            }
        )

    # ── Pass-set depth ────────────────────────────────────────────────────
    if throw_frame is not None:
        pass_set_depths = _compute_pass_set_depth(ol_tracklets, snap_frame, throw_frame)
        for psd in pass_set_depths:
            metrics.append(
                {
                    "metric_name": "pass_set_depth",
                    "metric_value": psd,
                    "unit": "yd",
                    "is_suppressed": suppressed,
                    "suppression_reason": reason,
                }
            )

    # ── Backside pursuit cutoff ───────────────────────────────────────────
    pursuit_results = _detect_pursuit_cutoff(
        tracklets,
        snap_frame,
        los_x,
        fps,
    )
    for pr in pursuit_results:
        metrics.append(
            {
                "metric_name": "pursuit_cutoff",
                "metric_value": pr,
                "unit": "yd",
                "is_suppressed": suppressed,
                "suppression_reason": reason,
            }
        )

    # Write metrics
    metric_ids: list[str] = []
    synthetic = any(
        (t.get("provenance") or {}).get("source") == "synthetic_test"
        or any(p.get("source") == "synthetic_test" for p in t.get("track_points", []))
        for t in tracklets
    )
    for m in metrics:
        check_control()
        m["experimental_flag"] = True
        m["metric_value"].update(
            definition_version="2026-10-21.1",
            measurement="tracking_observation",
            provenance={"source": "synthetic_test" if synthetic else "real", "validated": False},
        )
        m["metric_value"].setdefault(
            "required_evidence",
            [
                "field_calibration",
                "snap",
                "resolved_unit",
                "supported_film_model_validation",
            ],
        )
        m["is_suppressed"] = suppressed
        m["suppression_reason"] = reason
        try:
            resp = backend.create_metric(
                clip_id=clip_id,
                metric_name=m["metric_name"],
                metric_value=m["metric_value"],
                tracklet_id=m.get("tracklet_id"),
                unit=m.get("unit"),
                is_suppressed=m.get("is_suppressed", False),
                suppression_reason=m.get("suppression_reason"),
                job_id=job_id,
                experimental_flag=True,
            )
            metric_ids.append(resp["id"])
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning("oline_metric_write_failed", name=m["metric_name"], error=str(exc))
            raise

    log.info("stage_oline_done", clip_id=clip_id, metric_count=len(metric_ids))
    return {"metric_count": len(metric_ids), "metric_ids": metric_ids, "metrics": metrics}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _estimate_los(
    tracklets: list[dict[str, Any]], snap_frame: int, fps: float = 30
) -> float | None:
    xs: list[float] = []
    for t in tracklets:
        point = _position_at_frame(t, snap_frame, fps)
        if point is not None:
            xs.append(point[0])
    if not xs:
        return None
    xs.sort()
    return xs[len(xs) // 2]


def _position_at_frame(
    tracklet: dict[str, Any], frame: int, fps: float = 30
) -> tuple[float, float] | None:
    return field_position_at(tracklet, frame, fps)


def _identify_ol(
    tracklets: list[dict[str, Any]], snap_frame: int, los_x: float, fps: float = 30
) -> list[dict[str, Any]]:
    """Identify OL as the 5 players closest to LOS on the offensive side."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    for t in tracklets:
        if t.get("team_label") != "offense":
            continue
        pos = _position_at_frame(t, snap_frame, fps)
        if pos is None:
            continue
        # OL should be within a few yards of LOS on the offensive side
        depth = los_x - pos[0]
        if -OL_LOS_RANGE_YD <= depth <= OL_LOS_RANGE_YD:
            candidates.append((pos[1], t))
    if len(candidates) < 5:
        return []
    lateral_center = sorted(c[0] for c in candidates)[len(candidates) // 2]
    candidates = [(abs(y - lateral_center), t) for y, t in candidates]
    candidates.sort(key=lambda c: c[0])
    return [c[1] for c in candidates[:5]]


def _identify_dl(
    tracklets: list[dict[str, Any]], snap_frame: int, los_x: float, fps: float = 30
) -> list[dict[str, Any]]:
    """Identify DL as defenders within DL_LOS_RANGE_YD of LOS."""
    result: list[dict[str, Any]] = []
    for t in tracklets:
        if t.get("team_label") != "defense":
            continue
        pos = _position_at_frame(t, snap_frame, fps)
        if pos is None:
            continue
        depth = pos[0] - los_x
        if abs(depth) <= DL_LOS_RANGE_YD:
            result.append(t)
    return result


def _gap_measurements(
    tracks: list[dict[str, Any]], snap_frame: int, frame: int, fps: float
) -> list[dict[str, Any]]:
    # The initial neighbors define each spacing pair for the entire play.
    # Pullers crossing later cannot rename a formation's original gaps.
    ordered = sorted(
        ((_position_at_frame(t, snap_frame, fps), t) for t in tracks),
        key=lambda item: item[0][1] if item[0] else float("inf"),
    )
    measurements = []
    for index, ((initial_a, left), (initial_b, right)) in enumerate(zip(ordered, ordered[1:])):
        if initial_a is None or initial_b is None:
            continue
        a, b = _position_at_frame(left, frame, fps), _position_at_frame(right, frame, fps)
        if a is None or b is None:
            continue
        measurements.append(
            {
                "gap_index": index,
                "width_yards": round(math.dist(a, b), 3),
                "at_frame": frame,
                "source_start_frame": snap_frame,
                "initial_pair": [left.get("id"), right.get("id")],
                "geometry": "center_to_center",
                "clear_lane_width": None,
            }
        )
    return measurements


def _compute_gap_widths(ol_tracklets: list[dict[str, Any]], frame: int) -> list[float]:
    """Compute lateral gap widths between adjacent OL at the given frame."""
    positions: list[tuple[float, float]] = []
    for t in ol_tracklets:
        pos = _position_at_frame(t, frame)
        if pos is not None:
            positions.append(pos)
    if len(positions) < 2:
        return []
    positions.sort(key=lambda p: p[1])
    return [
        math.sqrt(
            (positions[i + 1][0] - positions[i][0]) ** 2
            + (positions[i + 1][1] - positions[i][1]) ** 2
        )
        for i in range(len(positions) - 1)
    ]


def _spacing_series(
    left: dict[str, Any], right: dict[str, Any], snap_frame: int, fps: float
) -> list[dict[str, Any]]:
    by_frame = {p["frame_number"]: p for t in (left, right) for p in t.get("track_points", [])}
    series = []
    for frame, reference in sorted(by_frame.items()):
        if frame < snap_frame:
            continue
        timestamp = reference.get("timestamp_seconds")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            continue  # Series require source-clock evidence, never inferred elapsed times.
        a = field_position_at(left, frame, fps, timestamp_seconds=timestamp)
        b = field_position_at(right, frame, fps, timestamp_seconds=timestamp)
        if a is None or b is None:
            series.append({"timestamp_seconds": timestamp, "visibility_state": "lost"})
        else:
            series.append(
                {
                    "timestamp_seconds": timestamp,
                    "width_yards": round(math.dist(a, b), 3),
                    "shot_id": reference.get("shot_id"),
                    "visibility_state": "observed",
                }
            )
    return series


def _detect_double_teams(
    ol_tracklets: list[dict[str, Any]],
    dl_tracklets: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
) -> list[dict[str, Any]]:
    """Detect when two OL are blocking one DL within proximity threshold."""
    results: list[dict[str, Any]] = []
    check_frame = snap_frame + int(fps * 0.3)
    for dl in dl_tracklets:
        dl_pos = _position_at_frame(dl, check_frame)
        if dl_pos is None:
            continue
        engaged_ol: list[str] = []
        for ol in ol_tracklets:
            ol_pos = _position_at_frame(ol, check_frame)
            if ol_pos is None:
                continue
            dist = math.sqrt((ol_pos[0] - dl_pos[0]) ** 2 + (ol_pos[1] - dl_pos[1]) ** 2)
            if dist < DOUBLE_TEAM_PROXIMITY_YD:
                engaged_ol.append(ol.get("id", "unknown"))
        if len(engaged_ol) >= 2:
            # Estimate release timing: find when one OL moves away
            release_frame = _find_release_frame(
                ol_tracklets, dl, snap_frame, check_frame + int(fps * 2), fps
            )
            results.append(
                {
                    "dl_track_id": dl.get("id", "unknown"),
                    "ol_track_ids": engaged_ol,
                    "detected_at_frame": check_frame,
                    "release_frame": release_frame,
                    "confidence": 0.55,
                }
            )
    return results


def _find_release_frame(
    ol_tracklets: list[dict[str, Any]],
    dl_tracklet: dict[str, Any],
    start_frame: int,
    end_frame: int,
    fps: float,
) -> int | None:
    """Find the frame where one OL releases from a double-team."""
    for frame in range(start_frame, end_frame, max(1, int(fps * 0.1))):
        dl_pos = _position_at_frame(dl_tracklet, frame)
        if dl_pos is None:
            continue
        close_count = 0
        for ol in ol_tracklets:
            ol_pos = _position_at_frame(ol, frame)
            if ol_pos is None:
                continue
            dist = math.sqrt((ol_pos[0] - dl_pos[0]) ** 2 + (ol_pos[1] - dl_pos[1]) ** 2)
            if dist < DOUBLE_TEAM_PROXIMITY_YD:
                close_count += 1
        if close_count <= 1:
            return frame
    return None


def _detect_stunts(
    dl_tracklets: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
) -> list[dict[str, Any]]:
    """Detect DL stunts/twists by tracking lateral movement post-snap."""
    results: list[dict[str, Any]] = []
    window = int(fps * 1.0)
    for dl in dl_tracklets:
        snap_pos = _position_at_frame(dl, snap_frame)
        post_pos = _position_at_frame(dl, snap_frame + window)
        if snap_pos is None or post_pos is None:
            continue
        lateral_move = abs(post_pos[1] - snap_pos[1])
        if lateral_move > STUNT_LATERAL_THRESHOLD_YD:
            results.append(
                {
                    "track_id": dl.get("id", "unknown"),
                    "lateral_displacement_yards": round(lateral_move, 2),
                    "direction": "left" if post_pos[1] < snap_pos[1] else "right",
                    "confidence": 0.5,
                }
            )
    return results


def _compute_block_shed_timing(
    ol_tracklets: list[dict[str, Any]],
    dl_tracklets: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
) -> list[dict[str, Any]]:
    """Estimate block shed: when a DL separates from the nearest OL."""
    results: list[dict[str, Any]] = []
    max_frames = int(fps * 4.0)
    for dl in dl_tracklets:
        engagement_start: int | None = None
        shed_frame: int | None = None
        for frame_offset in range(0, max_frames, max(1, int(fps * 0.1))):
            frame = snap_frame + frame_offset
            dl_pos = _position_at_frame(dl, frame)
            if dl_pos is None:
                continue
            min_dist = float("inf")
            for ol in ol_tracklets:
                ol_pos = _position_at_frame(ol, frame)
                if ol_pos is None:
                    continue
                d = math.sqrt((ol_pos[0] - dl_pos[0]) ** 2 + (ol_pos[1] - dl_pos[1]) ** 2)
                min_dist = min(min_dist, d)
            if min_dist < DOUBLE_TEAM_PROXIMITY_YD:
                if engagement_start is None:
                    engagement_start = frame
            elif engagement_start is not None:
                shed_frame = frame
                break
        if engagement_start is not None and shed_frame is not None:
            shed_time = (shed_frame - engagement_start) / max(fps, 1)
            results.append(
                {
                    "track_id": dl.get("id", "unknown"),
                    "shed_time_seconds": round(shed_time, 2),
                    "engagement_start_frame": engagement_start,
                    "shed_frame": shed_frame,
                    "confidence": 0.5,
                }
            )
    return results


def _compute_pass_set_depth(
    ol_tracklets: list[dict[str, Any]],
    snap_frame: int,
    throw_frame: int,
) -> list[dict[str, Any]]:
    """Measure OL backward depth from snap to throw (pass protection set)."""
    results: list[dict[str, Any]] = []
    for ol in ol_tracklets:
        snap_pos = _position_at_frame(ol, snap_frame)
        throw_pos = _position_at_frame(ol, throw_frame)
        if snap_pos is None or throw_pos is None:
            continue
        depth = abs(throw_pos[0] - snap_pos[0])
        if depth > 0.5:
            results.append(
                {
                    "track_id": ol.get("id", "unknown"),
                    "depth_yards": round(depth, 2),
                    "confidence": 0.55,
                }
            )
    return results


def _detect_pursuit_cutoff(
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    los_x: float,
    fps: float,
) -> list[dict[str, Any]]:
    """Detect backside defenders pursuing ball carrier through cutoff angles."""
    results: list[dict[str, Any]] = []
    window = int(fps * 2.0)
    for t in tracklets:
        snap_pos = _position_at_frame(t, snap_frame)
        if snap_pos is None:
            continue
        # Only look at defenders (ahead of LOS at snap)
        if snap_pos[0] - los_x < 1.0:
            continue
        late_pos = _position_at_frame(t, snap_frame + window)
        if late_pos is None:
            continue
        # Pursuit = moving significantly forward (toward offense) and laterally
        forward = snap_pos[0] - late_pos[0]
        lateral = abs(late_pos[1] - snap_pos[1])
        if forward > 3.0 and lateral > 3.0:
            results.append(
                {
                    "track_id": t.get("id", "unknown"),
                    "forward_yards": round(forward, 2),
                    "lateral_yards": round(lateral, 2),
                    "pursuit_angle_degrees": round(math.degrees(math.atan2(lateral, forward)), 1),
                    "confidence": 0.5,
                }
            )
    return results
