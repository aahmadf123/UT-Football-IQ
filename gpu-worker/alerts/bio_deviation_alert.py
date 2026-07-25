"""Biomechanical deviation alert (Issue #16).

Flags reps where pose metrics deviate >2 SD from a player's rolling 4-week
baseline.  Returns an empty list if the pose pipeline is not active or if the
position group has not been coach-approved — per Issue #6 governance.

Alert governance (non-negotiable):
  - Alerts are NEVER shown to players.
  - Bio deviation alerts require Issue #6 pose pipeline active AND
    coach-approved for the position group.
  - Every alert carries: confidence, clip_uri, player_id, timestamp,
    position_group.
  - Thresholds are configurable per position group via alert_config.

Input format for pose_metrics (list of dicts):
    {
        "metric_name": str,   # e.g. "pose_pad_level"
        "value": float,       # the scalar value to compare
        "confidence": float,  # 0.0–1.0
    }

Input format for baseline_metrics (list of dicts):
    {
        "metric_name": str,
        "values": list[float],  # rolling 4-week historical values
    }
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_DEVIATION_THRESHOLD_SD: float = 2.0
DEFAULT_MIN_BASELINE_SAMPLES: int = 5


def run(
    player_id: str,
    clip_id: str,
    clip_uri: str,
    position_group: str,
    pose_metrics: list[dict[str, Any]],
    baseline_metrics: list[dict[str, Any]],
    *,
    pose_pipeline_active: bool = False,
    coach_approved: bool = False,
    deviation_threshold_sd: float = DEFAULT_DEVIATION_THRESHOLD_SD,
    min_baseline_samples: int = DEFAULT_MIN_BASELINE_SAMPLES,
) -> list[dict[str, Any]]:
    """Check pose metrics for deviations beyond the rolling 4-week baseline.

    Guards:
      1. pose_pipeline_active must be True (Issue #6 pipeline running).
      2. coach_approved must be True for this position group.
      3. baseline must have >= min_baseline_samples per metric.

    Args:
        player_id:             DB UUID of the player.
        clip_id:               DB UUID of the current clip.
        clip_uri:              Evidence clip URI.
        position_group:        e.g. "OL", "WR", "QB".
        pose_metrics:          Current-rep pose metric list.
        baseline_metrics:      Rolling 4-week baseline metric list.
        pose_pipeline_active:  Whether Issue #6 pose pipeline is active.
        coach_approved:        Position coach approved bio alerts for this group.
        deviation_threshold_sd: SD threshold (default 2.0).
        min_baseline_samples:  Minimum history length to compute SD.

    Returns:
        List of alert dicts; empty list if guards not met or no deviations.
    """
    if not pose_pipeline_active:
        log.debug("bio_deviation_skipped_no_pose_pipeline", player_id=player_id)
        return []

    if not coach_approved:
        log.debug("bio_deviation_skipped_not_approved", player_id=player_id, pg=position_group)
        return []

    if not pose_metrics or not baseline_metrics:
        return []

    # Build lookup: metric_name → list of historical float values
    baseline_lookup: dict[str, list[float]] = {}
    for b in baseline_metrics:
        name = b.get("metric_name", "")
        raw_values = b.get("values", [])
        if not name or not raw_values:
            continue
        floats = [float(v) for v in raw_values if v is not None]
        if len(floats) >= min_baseline_samples:
            baseline_lookup[name] = floats

    alerts: list[dict[str, Any]] = []
    timestamp = datetime.now(UTC).isoformat()

    for metric in pose_metrics:
        metric_name = metric.get("metric_name", "")
        metric_val = metric.get("value")
        confidence = float(metric.get("confidence", 0.0))

        if metric_name not in baseline_lookup or metric_val is None:
            continue

        baseline_vals = baseline_lookup[metric_name]

        try:
            mean = statistics.mean(baseline_vals)
            stdev = statistics.pstdev(baseline_vals)
        except statistics.StatisticsError:
            continue

        if stdev < 1e-6:
            continue

        deviation_sd = abs(float(metric_val) - mean) / stdev

        if deviation_sd < deviation_threshold_sd:
            continue

        severity = "high" if deviation_sd >= 3.0 else "medium"
        log.info(
            "bio_deviation_alert",
            player_id=player_id,
            metric_name=metric_name,
            deviation_sd=round(deviation_sd, 2),
            severity=severity,
        )
        alerts.append(
            {
                "alert_type": "bio_deviation",
                "player_id": player_id,
                "clip_id": clip_id,
                "clip_uri": clip_uri,
                "position_group": position_group,
                "metric_name": metric_name,
                "metric_value": round(float(metric_val), 4),
                "baseline_mean": round(mean, 4),
                "baseline_stdev": round(stdev, 4),
                "deviation_sd": round(deviation_sd, 3),
                "threshold_sd": deviation_threshold_sd,
                "confidence": round(confidence, 3),
                "severity": severity,
                "timestamp": timestamp,
            }
        )

    return alerts
