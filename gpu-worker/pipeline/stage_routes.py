"""Stage — Route Classification (Phase 2).

Classifies eligible receiver routes from tracking data:
  - Route tree: curl, post, dig, corner, go, slant, out, in, hitch, flat,
    screen, wheel, seam, drag, crossing, comeback
  - Route depth landmarks: short (<10 yd), intermediate (10-20 yd), deep (>20 yd)
  - Route combination tagging: flood, mesh, snag, smash, levels, drive,
    spacing, scissors, verticals
  - Review-first workflow: all proposals carry confidence + evidence

Heuristic approach (Phase 2 MVP):
  1. Identify eligible receivers (wide-side tracklets from LOS at snap).
  2. Extract post-snap trajectory relative to LOS.
  3. Classify route by trajectory shape (angle changes, depth, lateral movement).
  4. Detect route combinations by grouping receiver routes per play side.

Output: `labels` rows with label_type in {route_family, route_depth,
route_combination} written to the backend.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from pipeline import backend
from pipeline.runtime import PipelineInterrupted, check_control

log = structlog.get_logger(__name__)

# Route angle thresholds (degrees from vertical)
_SLANT_ANGLE_MAX = 30.0
_OUT_ANGLE_MIN = 60.0
_DEEP_DEPTH_YD = 20.0
_INTERMEDIATE_DEPTH_YD = 10.0


def run(
    clip_id: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    fps: float,
) -> dict[str, Any]:
    """Classify routes for eligible receivers and write labels."""
    log.info("stage_routes_start", clip_id=clip_id)

    snap_event = next((e for e in events if e.get("event_type") == "snap"), None)
    if snap_event is None:
        return {"label_count": 0, "label_ids": [], "route_results": [], "combinations": [],
                "skipped": True, "reason": "snap_not_observed"}
    snap_frame = snap_event.get("frame_number", 0) if snap_event else 0

    end_event = next(
        (e for e in events if e.get("event_type") in ("end_of_play", "tackle")),
        None,
    )
    end_frame = end_event.get("frame_number") if end_event else None

    los_x = _estimate_los(tracklets, snap_frame)

    receiver_tracklets = _identify_receivers(tracklets, snap_frame, los_x)

    labels: list[dict[str, Any]] = []
    route_results: list[dict[str, Any]] = []

    for t in receiver_tracklets:
        trajectory = _extract_post_snap_trajectory(t, snap_frame, end_frame)
        if len(trajectory) < 3:
            continue

        route_name, confidence = _classify_route(trajectory, los_x)
        depth_yd = _route_depth(trajectory)
        depth_class = _depth_classification(depth_yd)
        track_id = t.get("id", "unknown")

        route_result = {
            "track_id": track_id,
            "route": route_name,
            "depth_yards": round(depth_yd, 1),
            "depth_class": depth_class,
            "confidence": confidence,
        }
        route_results.append(route_result)

        labels.append({
            "label_type": "route_family",
            "label_value": {
                "route": route_name,
                "confidence": confidence,
                "track_id": track_id,
                "depth_yards": round(depth_yd, 1),
            },
        })
        labels.append({
            "label_type": "route_depth",
            "label_value": {
                "depth_class": depth_class,
                "depth_yards": round(depth_yd, 1),
                "track_id": track_id,
            },
        })

    # Route combination detection
    combos = _detect_route_combinations(route_results)
    for combo in combos:
        labels.append({
            "label_type": "route_combination",
            "label_value": combo,
        })

    # Write labels
    label_ids: list[str] = []
    for lbl in labels:
        check_control()
        lbl["label_value"].update(source="heuristic", experimental=True, review_required=True,
                                  provenance={"source": "heuristic", "validated": False})
        try:
            resp = backend.create_label(
                lbl["label_type"],
                lbl["label_value"],
                clip_id=clip_id,
                source="model",
            )
            label_ids.append(resp["id"])
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning("route_label_write_failed", label_type=lbl["label_type"], error=str(exc))
            raise

    log.info("stage_routes_done", clip_id=clip_id, label_count=len(label_ids))
    return {
        "label_count": len(label_ids),
        "label_ids": label_ids,
        "route_results": route_results,
        "combinations": combos,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _estimate_los(
    tracklets: list[dict[str, Any]], snap_frame: int
) -> float:
    """Estimate line of scrimmage as the median x-position at snap."""
    xs: list[float] = []
    for t in tracklets:
        for pt in t.get("track_points", []):
            if pt.get("frame_number") == snap_frame:
                fx = pt.get("field_x")
                if fx is not None:
                    xs.append(float(fx))
                break
    if not xs:
        return 50.0
    xs.sort()
    return xs[len(xs) // 2]


def _identify_receivers(
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    los_x: float,
) -> list[dict[str, Any]]:
    """Identify eligible receivers: players wide of the tackle box at snap."""
    receivers: list[dict[str, Any]] = []
    for t in tracklets:
        snap_pt = None
        for pt in t.get("track_points", []):
            if pt.get("frame_number") == snap_frame:
                snap_pt = pt
                break
        if snap_pt is None:
            continue
        fx = snap_pt.get("field_x")
        fy = snap_pt.get("field_y")
        if fx is None or fy is None:
            continue
        # Receivers are typically wider than ±8 yards from the ball (tackle box)
        # or behind the LOS (backfield eligible receivers)
        if abs(float(fy)) > 8.0 or float(fx) < los_x - 3.0:
            receivers.append(t)
    return receivers


def _extract_post_snap_trajectory(
    tracklet: dict[str, Any],
    snap_frame: int,
    end_frame: int | None,
) -> list[tuple[float, float]]:
    """Extract field (x, y) positions after the snap."""
    pts = sorted(tracklet.get("track_points", []), key=lambda p: p.get("frame_number", 0))
    trajectory: list[tuple[float, float]] = []
    for pt in pts:
        fn = pt.get("frame_number", 0)
        if fn < snap_frame:
            continue
        if end_frame is not None and fn > end_frame:
            break
        fx = pt.get("field_x")
        fy = pt.get("field_y")
        if fx is not None and fy is not None:
            trajectory.append((float(fx), float(fy)))
    return trajectory


def _classify_route(
    trajectory: list[tuple[float, float]],
    los_x: float,
) -> tuple[str, float]:
    """Classify a route from its post-snap trajectory."""
    if len(trajectory) < 3:
        return ("unknown", 0.3)

    start_x, start_y = trajectory[0]
    end_x, end_y = trajectory[-1]

    # Total depth (forward from LOS)
    depth = abs(end_x - start_x)
    # Lateral displacement
    lateral = end_y - start_y

    # Find the deepest point in the route (for comeback/curl detection)
    max_depth_idx = 0
    max_depth = 0.0
    for i, (x, _y) in enumerate(trajectory):
        d = abs(x - start_x)
        if d > max_depth:
            max_depth = d
            max_depth_idx = i

    # Check for a break (significant direction change)
    break_detected = max_depth_idx < len(trajectory) - 3 and max_depth > depth + 2.0

    # Primary angle from start to end
    angle_rad = math.atan2(abs(lateral), depth) if depth > 0.5 else math.pi / 2
    angle_deg = math.degrees(angle_rad)

    # Classification logic
    if depth < 3.0 and abs(lateral) < 3.0:
        return ("screen", 0.55)

    if depth < 5.0 and abs(lateral) > 5.0:
        return ("flat", 0.6)

    if break_detected and max_depth > 10.0:
        # Route with a significant comeback
        if abs(lateral) < 3.0:
            return ("curl", 0.65)
        if lateral > 0:
            return ("comeback", 0.6)
        return ("comeback", 0.6)

    if depth > _DEEP_DEPTH_YD:
        if angle_deg < _SLANT_ANGLE_MAX:
            return ("go", 0.7)
        if angle_deg > _OUT_ANGLE_MIN:
            return ("corner", 0.6) if (lateral > 0) == (start_y > 0) else ("post", 0.6)
        return ("seam", 0.6)

    if depth > _INTERMEDIATE_DEPTH_YD:
        if abs(lateral) > 8.0:
            return ("dig", 0.6) if abs(lateral) > abs(depth) else ("in", 0.55)
        if angle_deg < _SLANT_ANGLE_MAX:
            return ("seam", 0.6)
        return ("crossing", 0.55)

    # Short routes
    if angle_deg < _SLANT_ANGLE_MAX:
        return ("hitch", 0.6)

    if angle_deg > _OUT_ANGLE_MIN:
        return ("out", 0.6) if lateral * start_y > 0 else ("slant", 0.6)

    return ("drag", 0.5)


def _route_depth(trajectory: list[tuple[float, float]]) -> float:
    """Calculate route depth in yards from the start position."""
    if len(trajectory) < 2:
        return 0.0
    start_x = trajectory[0][0]
    max_depth = max(abs(x - start_x) for x, _ in trajectory)
    return max_depth


def _depth_classification(depth_yd: float) -> str:
    if depth_yd > _DEEP_DEPTH_YD:
        return "deep"
    if depth_yd > _INTERMEDIATE_DEPTH_YD:
        return "intermediate"
    return "short"


def _detect_route_combinations(
    route_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect common route combinations from classified routes."""
    if len(route_results) < 2:
        return []

    routes = {r["route"] for r in route_results}
    combos: list[dict[str, Any]] = []

    # Flood: deep + intermediate + flat/short on the same side
    depth_classes = {r["depth_class"] for r in route_results}
    if "deep" in depth_classes and "short" in depth_classes and len(route_results) >= 3:
        combos.append({
            "combination": "flood",
            "confidence": 0.55,
            "routes": [r["route"] for r in route_results],
        })

    # Mesh: two crossing/drag routes
    crossing_count = sum(1 for r in route_results if r["route"] in ("crossing", "drag"))
    if crossing_count >= 2:
        combos.append({
            "combination": "mesh",
            "confidence": 0.6,
            "routes": [r["route"] for r in route_results if r["route"] in ("crossing", "drag")],
        })

    # Smash: corner + hitch/curl combination
    if ("corner" in routes or "post" in routes) and ("hitch" in routes or "curl" in routes):
        combos.append({
            "combination": "smash",
            "confidence": 0.6,
            "routes": [r["route"] for r in route_results],
        })

    # Snag: flat + curl/hitch + corner/seam
    if "flat" in routes and ("curl" in routes or "hitch" in routes):
        combos.append({
            "combination": "snag",
            "confidence": 0.55,
            "routes": [r["route"] for r in route_results],
        })

    # Levels: routes at different depths on the same side
    if len(depth_classes) >= 2 and "deep" not in depth_classes:
        combos.append({
            "combination": "levels",
            "confidence": 0.5,
            "routes": [r["route"] for r in route_results],
        })

    # Verticals: multiple go/seam routes
    vert_count = sum(1 for r in route_results if r["route"] in ("go", "seam"))
    if vert_count >= 2:
        combos.append({
            "combination": "verticals",
            "confidence": 0.6,
            "routes": [r["route"] for r in route_results if r["route"] in ("go", "seam")],
        })

    return combos
