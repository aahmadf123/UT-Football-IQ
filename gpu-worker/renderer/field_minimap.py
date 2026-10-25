"""Small calibrated NCAA field view for exported overlay videos."""

from __future__ import annotations

import math
from typing import Any

import cv2

from pipeline.homography.field_template import default_template

TEMPLATE = default_template()
END_ZONE_YD = 10.0
FIELD_LENGTH_YD = TEMPLATE.length + 2 * END_ZONE_YD
FIELD_WIDTH_YD = TEMPLATE.width


def field_to_minimap(x: float, y: float, rectangle: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """Ground-plane yards to the inset; unknown/out-of-field points stay absent."""
    # Canonical homography frame: goal lines x=0/100, centered y=+-26.665.
    if (not math.isfinite(x + y) or not -END_ZONE_YD <= x <= TEMPLATE.length + END_ZONE_YD
            or not -TEMPLATE.half_width <= y <= TEMPLATE.half_width):
        return None
    left, top, width, height = rectangle
    return (round(left + (x + END_ZONE_YD) / FIELD_LENGTH_YD * width),
            round(top + (y + TEMPLATE.half_width) / FIELD_WIDTH_YD * height))


def draw_field_minimap(frame: Any, observations: list[dict[str, Any]], *,
                       width_fraction: float = 0.22) -> None:
    """Opaque compact field; draw only supplied fresh calibrated player observations."""
    height, width = frame.shape[:2]
    if width < 32 or height < 32:
        return
    margin = max(4, round(width * 0.012))
    map_width = min(200, max(72, round(width * width_fraction)), width - 2 * margin - 6,
                    int((height - 2 * margin - 6) * FIELD_LENGTH_YD / FIELD_WIDTH_YD))
    if map_width < 16:
        return
    map_height = round(map_width * FIELD_WIDTH_YD / FIELD_LENGTH_YD)
    left, top = width - map_width - margin, height - map_height - margin
    rectangle = (left, top, map_width, map_height)
    cv2.rectangle(frame, (left - 3, top - 3), (left + map_width + 3, top + map_height + 3), (32, 21, 11), -1)
    cv2.rectangle(frame, (left, top), (left + map_width, top + map_height), (48, 60, 24), -1)
    cv2.rectangle(frame, (left, top), (left + map_width, top + map_height), (160, 178, 169), 1)
    for yard in range(10, 111, 10):
        x = round(left + map_width * yard / FIELD_LENGTH_YD)
        cv2.line(frame, (x, top), (x, top + map_height), (108, 129, 117), 1)
        # NCAA hashes are 60 feet in from either sideline (20 yards).
        for lateral in (20, FIELD_WIDTH_YD - 20):
            y = round(top + map_height * lateral / FIELD_WIDTH_YD)
            cv2.line(frame, (x - 1, y), (x + 1, y), (185, 202, 192), 1)
    colors = {"offense": (0, 205, 255), "defense": (235, 145, 45), "unknown": (235, 235, 235)}
    for point in observations:
        if (point.get("team_label") in {"official", "sideline"}
                or point.get("actor_kind") in {"official", "sideline", "unknown"}):
            continue
        x, y = point.get("field_x"), point.get("field_y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        position = field_to_minimap(float(x), float(y), rectangle)
        if position is None:
            continue
        color = colors.get(point.get("team_label"), colors["unknown"])
        cv2.circle(frame, position, 3, (32, 21, 11), -1)
        cv2.circle(frame, position, 2, color, -1)
