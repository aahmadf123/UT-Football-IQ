"""Bounded source-clock sampling shared by observed geometry producers."""

from __future__ import annotations

import math
from bisect import bisect_left
from typing import Any


def field_position_at(track: dict[str, Any], frame: int, fps: float = 30,
                      *, max_gap_seconds: float = 0.2,
                      endpoint_seconds: float = 0.1,
                      timestamp_seconds: float | None = None) -> tuple[float, float] | None:
    """Interpolate a short same-shot observation gap, never a lost interval."""
    if fps <= 0:
        return None
    points = sorted((p for p in track.get("track_points", []) if "frame_number" in p),
                    key=lambda p: p["frame_number"])
    if not points:
        return None

    def coordinates(point: dict[str, Any]) -> tuple[float, float] | None:
        if point.get("visibility_state") in {"lost", "offscreen", "occluded"}:
            return None
        if point.get("field_x") is None or point.get("field_y") is None:
            return None
        x, y = float(point["field_x"]), float(point["field_y"])
        return (x, y) if math.isfinite(x) and math.isfinite(y) else None

    index = bisect_left([p["frame_number"] for p in points], frame)
    if index < len(points) and points[index]["frame_number"] == frame:
        return coordinates(points[index])
    if index == 0 or index == len(points):
        endpoint = points[0] if index == 0 else points[-1]
        elapsed = abs(endpoint["frame_number"] - frame) / fps
        if timestamp_seconds is not None and endpoint.get("timestamp_seconds") is not None:
            elapsed = abs(endpoint["timestamp_seconds"] - timestamp_seconds)
        return (coordinates(endpoint)
                if elapsed <= endpoint_seconds else None)
    before, after = points[index - 1], points[index]
    a, b = coordinates(before), coordinates(after)
    elapsed = (after["frame_number"] - before["frame_number"]) / fps
    if before.get("timestamp_seconds") is not None and after.get("timestamp_seconds") is not None:
        elapsed = after["timestamp_seconds"] - before["timestamp_seconds"]
    if (a is None or b is None or before.get("shot_id") != after.get("shot_id")
            or not 0 < elapsed <= max_gap_seconds):
        return None
    fraction = (frame - before["frame_number"]) / (after["frame_number"] - before["frame_number"])
    if before.get("timestamp_seconds") is not None and after.get("timestamp_seconds") is not None:
        if timestamp_seconds is not None:
            fraction = (timestamp_seconds - before["timestamp_seconds"]) / elapsed
        elif abs(elapsed - (after["frame_number"] - before["frame_number"]) / fps) > 0.001:
            # A VFR target timestamp cannot be reconstructed from frame number.
            return None
    if not 0 <= fraction <= 1:
        return None
    return a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1])


def bounded_frame_observations(track: dict[str, Any], timeline: Any,
                               fps: float) -> list[dict[str, Any]]:
    """Fill at most 200 ms between same-shot boxes on the source PTS clock.

    Never extrapolate a lost endpoint. This also keeps a 30-Hz detector visible
    on a 59.94-Hz source resampled to a 30-Hz exported overlay: those two sample
    grids do not have identical frame numbers.
    """
    output = []
    previous = None

    def valid(point: dict[str, Any]) -> bool:
        return (isinstance(point.get("bbox"), (list, tuple)) and len(point["bbox"]) == 4
                and all(isinstance(v, (int, float)) and math.isfinite(v) for v in point["bbox"])
                and point.get("visibility_state") not in {"lost", "offscreen", "occluded"})

    def timestamp(point: dict[str, Any]) -> float:
        return (timeline.timestamp(point["frame_number"]) if timeline is not None
                else point.get("timestamp_seconds") if point.get("timestamp_seconds") is not None
                else point["frame_number"] / fps)

    for point in sorted(track.get("track_points", []), key=lambda p: p["frame_number"]):
        if valid(point):
            if previous is not None and valid(previous) and previous.get("shot_id") == point.get("shot_id"):
                before, after = timestamp(previous), timestamp(point)
                if 0 < after - before <= 0.2:
                    for frame in range(previous["frame_number"] + 1, point["frame_number"]):
                        current = (timeline.timestamp(frame) if timeline is not None else frame / fps)
                        fraction = (current - before) / (after - before)
                        if 0 <= fraction <= 1:
                            interpolated = {"frame_number": frame, "interpolated": True,
                                            "bbox": [a + fraction * (b - a) for a, b in
                                                     zip(previous["bbox"], point["bbox"])]}
                            for coordinate in ("field_x", "field_y"):
                                a, b = previous.get(coordinate), point.get(coordinate)
                                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                                    interpolated[coordinate] = a + fraction * (b - a)
                            output.append(interpolated)
            output.append({**point, "interpolated": False})
        previous = point
    return output
