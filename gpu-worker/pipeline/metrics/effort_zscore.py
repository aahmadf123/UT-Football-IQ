"""Effort review candidate scoring for American-football tracklets.

The output is deliberately framed as a coach-review candidate, not as a
definitive player judgment. Low-confidence identity stays attached to the
anonymous track/role so uncertain CV output does not become a player label.
"""

from __future__ import annotations

import math
from typing import Any

LOW_IDENTITY_CONFIDENCE = 0.70
LOW_TRACKING_CONFIDENCE = 0.60
LOAF_SPEED_RATIO = 0.50
LOAF_CONSECUTIVE_FRAMES = 5
SKILL_GROUPS = {"SKILL", "WR", "RB", "TE", "SLOT"}
SKILL_POSITIONS = {"WR", "RB", "HB", "FB", "TE", "SLOT"}


def evaluate_effort_candidate(
    tracklet: dict[str, Any],
    *,
    snap_frame: int,
    fps: float,
    analytics_safe: bool,
) -> dict[str, Any] | None:
    """Return an effort review candidate for one tracklet.

    Expected baseline inputs can be supplied either as flat tracklet fields or
    inside ``tracklet["effort_baseline"]``:

    - ``mean_max_speed_yps`` / ``baseline_max_speed_mean_yps``
    - ``std_max_speed_yps`` / ``baseline_max_speed_std_yps``
    - ``avg_speed_yps`` / ``baseline_avg_speed_yps``
    """

    pts = sorted(tracklet.get("track_points", []), key=lambda p: p.get("frame_number", 0))
    speeds = _frame_speeds(pts, fps)
    if not speeds:
        return None

    track_id = str(tracklet.get("id", "unknown"))
    position = str(tracklet.get("position") or "").upper()
    position_group = str(tracklet.get("position_group") or "").upper()
    identity_confidence = _confidence(tracklet, ("identity_confidence", "player_identity_confidence", "reid_confidence"))
    tracking_confidence = _confidence(tracklet, ("track_confidence", "tracking_confidence"))
    baseline = tracklet.get("effort_baseline") if isinstance(tracklet.get("effort_baseline"), dict) else {}

    baseline_mean = _number(
        tracklet.get("baseline_max_speed_mean_yps"),
        tracklet.get("mean_max_speed_yps"),
        baseline.get("mean_max_speed_yps"),
        baseline.get("baseline_max_speed_mean_yps"),
    )
    baseline_std = _number(
        tracklet.get("baseline_max_speed_std_yps"),
        tracklet.get("std_max_speed_yps"),
        baseline.get("std_max_speed_yps"),
        baseline.get("baseline_max_speed_std_yps"),
    )
    baseline_avg = _number(
        tracklet.get("baseline_avg_speed_yps"),
        tracklet.get("avg_speed_yps"),
        baseline.get("avg_speed_yps"),
        baseline.get("baseline_avg_speed_yps"),
    )

    max_speed = max(speed for _, speed in speeds)
    effort_zscore: float | None = None
    reason_codes: list[str] = []

    if baseline_mean is None:
        reason_codes.append("baseline_mean_missing")
    if baseline_std is None or baseline_std <= 0:
        reason_codes.append("baseline_std_missing")
    elif baseline_mean is not None:
        effort_zscore = (max_speed - baseline_mean) / baseline_std

    is_skill = position_group in SKILL_GROUPS or position in SKILL_POSITIONS
    if not is_skill:
        reason_codes.append("non_skill_tracklet")

    loaf_flag: bool | None = None
    low_speed_streak = 0
    if baseline_avg is None or baseline_avg <= 0:
        reason_codes.append("baseline_avg_missing")
    else:
        threshold = baseline_avg * LOAF_SPEED_RATIO
        low_speed_streak = _low_speed_streak_after_snap(speeds, snap_frame, threshold)
        loaf_flag = bool(is_skill and low_speed_streak >= LOAF_CONSECUTIVE_FRAMES)
        if loaf_flag:
            reason_codes.append("possible_effort_drop")

    player_id = tracklet.get("player_id")
    attribution = "player" if player_id else "anonymous_track"
    if player_id is None:
        reason_codes.append("anonymous_track")
    if identity_confidence is None or identity_confidence < LOW_IDENTITY_CONFIDENCE:
        reason_codes.append("low_identity_confidence")
        attribution = "anonymous_track"
        player_id = None
    if tracking_confidence is None or tracking_confidence < LOW_TRACKING_CONFIDENCE:
        reason_codes.append("low_tracking_confidence")
    if not analytics_safe:
        reason_codes.append("calibration_not_safe")

    confidence = _combined_confidence(identity_confidence, tracking_confidence, analytics_safe)
    needs_review = bool(
        loaf_flag
        or attribution == "anonymous_track"
        or not analytics_safe
        or "low_tracking_confidence" in reason_codes
        or effort_zscore is None
    )

    return {
        "track_id": track_id,
        "player_id": str(player_id) if player_id is not None else None,
        "attribution": attribution,
        "position": position or None,
        "position_group": position_group or None,
        "max_speed_yps": round(max_speed, 3),
        "baseline_mean_yps": _round_optional(baseline_mean),
        "baseline_std_yps": _round_optional(baseline_std),
        "baseline_avg_speed_yps": _round_optional(baseline_avg),
        "effort_zscore": _round_optional(effort_zscore),
        "loaf_flag": loaf_flag,
        "low_speed_streak_frames": low_speed_streak,
        "identity_confidence": _round_optional(identity_confidence),
        "tracking_confidence": _round_optional(tracking_confidence),
        "confidence": round(confidence, 3),
        "review_state": "needs_coach_review" if needs_review else "informational",
        "reason_codes": sorted(set(reason_codes)),
        "label": "possible_effort_drop" if loaf_flag else "effort_within_expected_range",
    }


def _frame_speeds(pts: list[dict[str, Any]], fps: float) -> list[tuple[int, float]]:
    speeds: list[tuple[int, float]] = []
    for i in range(1, len(pts)):
        prev = pts[i - 1]
        cur = pts[i]
        f0 = int(prev.get("frame_number", 0) or 0)
        f1 = int(cur.get("frame_number", 0) or 0)
        dt = (f1 - f0) / max(fps, 1.0)
        if dt <= 0:
            continue
        x0 = _number(prev.get("field_x"))
        y0 = _number(prev.get("field_y"))
        x1 = _number(cur.get("field_x"))
        y1 = _number(cur.get("field_y"))
        if x0 is None or y0 is None or x1 is None or y1 is None:
            continue
        dist = math.hypot(x1 - x0, y1 - y0)
        speeds.append((f1, dist / dt))
    return speeds


def _low_speed_streak_after_snap(
    speeds: list[tuple[int, float]],
    snap_frame: int,
    threshold_yps: float,
) -> int:
    longest = 0
    current = 0
    for frame, speed in speeds:
        if frame <= snap_frame:
            continue
        if speed < threshold_yps:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _confidence(tracklet: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    return _number(*(tracklet.get(key) for key in keys))


def _number(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _combined_confidence(
    identity_confidence: float | None,
    tracking_confidence: float | None,
    analytics_safe: bool,
) -> float:
    parts = [
        identity_confidence if identity_confidence is not None else 0.0,
        tracking_confidence if tracking_confidence is not None else 0.0,
        1.0 if analytics_safe else 0.25,
    ]
    return max(0.0, min(1.0, sum(parts) / len(parts)))


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 3)
