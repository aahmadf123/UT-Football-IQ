"""Censor-aware QB event durations; proximity never implies pressure."""

from __future__ import annotations

import math
from typing import Any

from pipeline.video_timeline import VideoTimeline


def observed_qb_durations(tracklets: list[dict[str, Any]], events: list[dict[str, Any]],
                          fps: float, timeline: VideoTimeline | None = None) -> list[dict[str, Any]]:
    """Return holding/first-pressure intervals only when QB evidence exists.

    A track is a QB only through an explicit observed role or event identity.
    Missing endpoints produce right-censored intervals, never completed zeroes.
    """
    snap = next((e for e in events if e.get("event_type") == "snap"), None)
    if not snap or fps <= 0:
        return []

    def event_time(event: dict[str, Any]) -> float | None:
        value = event.get("timestamp_seconds")
        if value is None and timeline is not None and event.get("frame_number") is not None:
            value = timeline.timestamp(event["frame_number"])
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None

    start = event_time(snap)
    if start is None:
        return []
    qb_ids = {str((e.get("attributes") or {}).get("qb_id")) for e in events
              if (e.get("attributes") or {}).get("qb_id") is not None}
    quarterbacks = [t for t in tracklets if str(t.get("id")) in qb_ids
                    or (t.get("observed_role") == "QB" and t.get("role_resolved") is True)]
    if len(quarterbacks) != 1:
        return []
    qb = quarterbacks[0]
    observations = qb.get("track_points") or []
    observed_end = max((p["timestamp_seconds"] for p in observations
                        if isinstance(p.get("timestamp_seconds"), (int, float))), default=None)
    if observed_end is None or observed_end < start:
        return []
    endpoints = [(event_time(e), e) for e in events
                 if e.get("event_type") in {"throw", "pass_release", "sack", "scramble_start"}]
    endpoints = [(time, e) for time, e in endpoints if time is not None and time >= start]
    endpoint_time, endpoint = min(endpoints, key=lambda item: item[0]) if endpoints else (observed_end, None)
    endpoint_time = min(endpoint_time, observed_end)
    censored = endpoint is None or event_time(endpoint) > observed_end
    value = {"seconds": round(endpoint_time - start, 3), "right_censored": censored,
             "endpoint": "last_observation" if censored else endpoint["event_type"],
             "source_start_seconds": start, "source_end_seconds": endpoint_time,
             "track_id": qb["id"], "definition_version": "2026-10-21.1",
             "required_evidence": ["resolved_qb", "snap", "terminal_event_or_censor_time"]}
    metrics = [{"metric_name": "ball_holding_duration", "metric_value": value,
                "tracklet_id": qb["id"], "unit": "s", "is_suppressed": False}]
    # An accepted pressure event can be earlier than release even if the
    # rusher later retreats. Preserve the FIRST qualified onset in the window.
    pressures = [(event_time(e), e) for e in events if e.get("event_type") == "pressure"
                 and (e.get("source") in {"human", "manual"}
                      or (e.get("provenance") or {}).get("validated") is True)]
    pressures = [(time, e) for time, e in pressures
                 if time is not None and start <= time <= endpoint_time]
    if pressures:
        time, _event = min(pressures, key=lambda item: item[0])
        metrics.append({"metric_name": "time_to_first_pressure", "tracklet_id": qb["id"],
                        "unit": "s", "is_suppressed": False,
                        "metric_value": {**value, "seconds": round(time - start, 3),
                                         "source_end_seconds": time, "right_censored": False,
                                         "endpoint": "qualified_pressure_onset"}})
    return metrics
