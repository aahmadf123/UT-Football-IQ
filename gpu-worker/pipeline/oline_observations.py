"""Measured LOS cross-sections; geometric spacing never becomes clear width."""

from __future__ import annotations

from typing import Any

from pipeline.position_evidence import (
    evidence,
    geometry_snapshot,
    named_gaps,
    original_front,
    row,
)
from pipeline.video_timeline import VideoTimeline


def measured_lanes(
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timeline: VideoTimeline | None = None,
    attacking_direction: int | None = None,
) -> list[dict[str, Any]]:
    """Maximum contiguous free width within each ORIGINAL named gap at the LOS.

    Each supplied body is a measured field-coordinate footprint. Complete
    defender occupancy must be explicitly observed for every geometry snapshot.
    Series points are discrete measurements; no interpolation across snapshots.
    """
    snaps = [e for e in events if e.get("event_type") == "snap"]
    if (
        len(snaps) != 1
        or (front := original_front(tracklets, snaps[0], timeline, attacking_direction)) is None
    ):
        return []
    snapshots = [
        s
        for e in events
        if (s := geometry_snapshot(e, front, tracklets, timeline))
        and s["timestamp_seconds"] >= front["start"]
    ]
    if not snapshots:
        return []
    snapshots.sort(key=lambda s: s["timestamp_seconds"])
    results = []
    for gap in named_gaps(front):
        series = []
        for snapshot in snapshots:
            intervals = _clear_intervals(
                gap["y_min"], gap["y_max"], front["los_x"], snapshot["bodies"]
            )
            series.append(
                {
                    "timestamp_seconds": snapshot["timestamp_seconds"],
                    "width_yards": max((b - a for a, b in intervals), default=0),
                    "clear_intervals": [{"y_min": a, "y_max": b} for a, b in intervals],
                    "event_id": snapshot["event"].get("id"),
                    "visibility_state": "observed",
                }
            )
        first = series[0]
        packet = {
            **gap,
            **first,
            "series": series,
            "series_interpolation": "none",
            "measurement_region": {
                "cross_section": "LOS",
                "los_x": front["los_x"],
                "y_min": gap["y_min"],
                "y_max": gap["y_max"],
            },
            "source_start_seconds": first["timestamp_seconds"],
            "source_end_seconds": series[-1]["timestamp_seconds"],
            "attacking_direction": front["attacking_direction"],
            "definition_basis": "largest_contiguous_free_interval_within_original_center_split",
            "occupancy_complete": True,
            "body_ids": sorted(snapshots[0]["bodies"]),
            "event_evidence": evidence([front["snap"], *[s["event"] for s in snapshots]], timeline),
            "required_evidence": [
                "original_lineman_roles",
                "qualified_snap",
                "qualified_los",
                "attacking_direction",
                "measured_field_body_extents",
                "complete_defender_occupancy",
                "source_timestamps",
                "width_validation",
            ],
        }
        results.append(row("clear_lane_width", packet, unit="yd"))
    return results


def _clear_intervals(low, high, los, bodies):
    occupied = sorted(
        (max(low, b["min_y"]), min(high, b["max_y"]))
        for b in bodies.values()
        if b["min_x"] <= los <= b["max_x"] and b["max_y"] > low and b["min_y"] < high
    )
    cursor, clear = low, []
    for a, b in occupied:
        if a > cursor:
            clear.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < high:
        clear.append((cursor, high))
    return clear
