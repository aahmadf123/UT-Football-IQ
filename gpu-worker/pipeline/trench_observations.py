"""Exact paired source samples for an explicitly observed blocking interaction."""

from __future__ import annotations

from typing import Any

from pipeline.position_evidence import canonical_shot, number, visible
from pipeline.video_timeline import VideoTimeline


def interaction_geometry(
    tracklets: list[dict[str, Any]],
    blocker: str,
    defender: str,
    first: float,
    last: float,
    shot: str,
    timeline: VideoTimeline | None,
    *,
    analytics_safe: bool,
) -> dict[str, Any] | None:
    """Missing actors stay missing; neither positions nor contacts are inferred.

    Duration endpoints remain useful without field geometry. Replay separately
    requires safe calibration and an actual native PTS for each supplied sample.
    """
    if not analytics_safe or timeline is None or not timeline.verified or last <= first:
        return None
    actors = {}
    clocks: dict[int, float] = {}
    reverse: dict[float, int] = {}
    for identifier in (blocker, defender):
        tracks = [track for track in tracklets if str(track.get("id")) == identifier]
        if len(tracks) != 1:
            return None
        points = {}
        previous = None
        for point in tracks[0].get("track_points", []):
            seconds = number(point.get("timestamp_seconds"))
            if seconds is None:
                return None
            if not first <= seconds <= last:
                continue
            frame = point.get("frame_number")
            if (
                type(frame) is not int
                or not 0 <= frame < len(timeline.timestamps)
                or abs(timeline.timestamp(frame) - seconds) > 1e-8
                or frame in points
                or previous is not None and frame <= previous
            ):
                return None
            seconds = timeline.timestamp(frame)
            if (frame in clocks and clocks[frame] != seconds) or (
                seconds in reverse and reverse[seconds] != frame
            ):
                return None
            clocks[frame], reverse[seconds] = seconds, frame
            previous = frame
            points[frame] = (
                {"field_x": point["field_x"], "field_y": point["field_y"]}
                if point.get("visibility_state") == "observed"
                and point.get("interpolated") is not True
                and point.get("predicted") is not True
                and canonical_shot(point.get("shot_id")) == shot
                and visible(point, field=True)
                else None
            )
        actors[identifier] = points
    if len(clocks) < 2 or not any(
        actors[blocker].get(frame) and actors[defender].get(frame) for frame in clocks
    ):
        return None
    return {
        "schema_version": 1,
        "coordinate_space": "field_yards",
        "clock_basis": "verified_native_pts",
        "calibration_safe": True,
        "series_interpolation": "none",
        "shot_id": shot,
        "blocker_id": blocker,
        "defender_id": defender,
        "source_start_seconds": first,
        "source_end_seconds": last,
        "samples": [
            {
                "frame_number": frame,
                "timestamp_seconds": seconds,
                "blocker": actors[blocker].get(frame),
                "defender": actors[defender].get(frame),
            }
            for frame, seconds in sorted(clocks.items())
        ],
    }
