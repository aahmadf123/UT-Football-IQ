"""Workload-fusion signals for injury-risk review (Issue #149, §5.13).

Per-clip sprint/speed/distance stats, the acute:chronic workload ratio
(ACWR = mean daily load over the trailing 7 days / mean daily load over the
trailing 28 days), and a deterministic, transparent injury-risk heuristic
(``acwr-asym-heuristic-v1`` — registered in the model router so a learned
model can replace it later without code changes).

Every output is an experimental sports-performance indicator for staff
review — never a diagnosis or a medical prediction. Low-confidence identity
stays attached to the anonymous track (same gating as effort scoring) so
uncertain CV output never becomes a named-player risk label.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any

from pipeline.metrics.effort_zscore import (
    LOW_IDENTITY_CONFIDENCE,
    LOW_TRACKING_CONFIDENCE,
    _combined_confidence,
    _confidence,
    _number,
    _round_optional,
)

# ── Thresholds (defaults mirrored in backend alert_config) ────────────────────

# High-speed-running threshold ≈ 6.0 m/s expressed in yards/second.
SPRINT_SPEED_YPS = 6.56
# A high-speed run must be sustained this long to count as a sprint.
MIN_SPRINT_DURATION_S = 0.5
# Issue #149 alert thresholds: AC > 1.5 or asymmetry > 1.3.
ACWR_HIGH_DEFAULT = 1.5
ASYMMETRY_HIGH_DEFAULT = 1.3
# Days of data required in the 28-day window before ACWR is trustworthy.
MIN_CHRONIC_DAYS = 14

# Heuristic shape: ACWR sweet spot 0.8–1.3 (Gabbett), ramp to 1.0 risk at 2.0;
# undertraining ramps to at most 0.5. Asymmetry ramps from 1.1 to 1.5.
_ACWR_SWEET_LOW = 0.8
_ACWR_SWEET_HIGH = 1.3
_ACWR_MAX_RISK_AT = 2.0
_ASYM_RISK_START = 1.1
_ASYM_MAX_RISK_AT = 1.5
_W_ACWR = 0.6
_W_ASYM = 0.4
_PARTIAL_SIGNAL_CAP = 0.6

MODEL_VARIANT = "acwr-asym-heuristic-v1"


def sprint_stats(
    track_points: list[dict[str, Any]],
    fps: float,
    *,
    sprint_speed_yps: float = SPRINT_SPEED_YPS,
    min_sprint_duration_s: float = MIN_SPRINT_DURATION_S,
) -> dict[str, Any] | None:
    """Sprint count, max speed, and distance for one tracklet's points.

    A sprint is a contiguous run of frame-to-frame speeds at or above
    ``sprint_speed_yps`` sustained for at least ``min_sprint_duration_s``.
    Returns ``None`` when there are no usable field-coordinate speeds.
    """
    pts = sorted(track_points, key=lambda p: p.get("frame_number", 0))
    intervals = _interval_speeds(pts, fps)
    if not intervals:
        return None

    sprint_count = 0
    segment_start: int | None = None
    segment_end: int | None = None
    distance = 0.0

    def _close_segment() -> int:
        if segment_start is None or segment_end is None:
            return 0
        duration = (segment_end - segment_start) / max(fps, 1.0)
        return 1 if duration >= min_sprint_duration_s else 0

    for f0, f1, speed in intervals:
        distance += speed * (f1 - f0) / max(fps, 1.0)
        if speed >= sprint_speed_yps:
            if segment_start is None:
                segment_start = f0
            segment_end = f1
        else:
            sprint_count += _close_segment()
            segment_start = None
            segment_end = None
    sprint_count += _close_segment()

    return {
        "sprint_count": sprint_count,
        "max_speed_yps": round(max(speed for _, _, speed in intervals), 3),
        "distance_yards": round(distance, 2),
    }


def compute_acwr(
    daily_loads: Mapping[Any, Any],
    as_of: dt.date | str,
    *,
    min_chronic_days: int = MIN_CHRONIC_DAYS,
) -> dict[str, Any]:
    """Acute:chronic workload ratio as of a given day.

    ``daily_loads`` maps day (``datetime.date`` or ISO string) → total load
    for that day (e.g. summed CV ``distance_traveled`` yards). Days absent
    from the mapping are treated as zero-load rest days, but ACWR is only
    reported once at least ``min_chronic_days`` days in the trailing 28-day
    window actually have data.
    """
    day = _as_date(as_of)
    if day is None:
        raise ValueError(f"compute_acwr: unparseable as_of date {as_of!r}")
    loads: dict[dt.date, float] = {}
    for key, value in daily_loads.items():
        parsed = _as_date(key)
        load = _number(value)
        if parsed is not None and load is not None:
            loads[parsed] = load

    reason_codes: list[str] = []
    window_28 = [loads.get(day - dt.timedelta(days=i), 0.0) for i in range(28)]
    window_7 = window_28[:7]
    days_with_data = sum(
        1 for i in range(28) if (day - dt.timedelta(days=i)) in loads
    )

    acute = sum(window_7) / 7.0
    chronic = sum(window_28) / 28.0

    acwr: float | None = None
    if days_with_data < min_chronic_days:
        reason_codes.append("insufficient_chronic_history")
    elif chronic <= 0:
        reason_codes.append("no_chronic_load")
    else:
        acwr = acute / chronic

    return {
        "acute_load_7d": round(acute, 2),
        "chronic_load_28d": round(chronic, 2),
        "acwr": _round_optional(acwr),
        "days_with_data": days_with_data,
        "reason_codes": reason_codes,
    }


def compute_injury_risk(
    acwr: float | None,
    asymmetry_index: float | None,
) -> dict[str, Any]:
    """Deterministic injury-risk heuristic (``acwr-asym-heuristic-v1``).

    Returns a 0–1 experimental risk score plus the reason codes that explain
    it. With only one signal available the score is capped so a single noisy
    input can never look like a confident high-risk call.
    """
    reason_codes: list[str] = []

    r_acwr: float | None = None
    if acwr is None:
        reason_codes.append("acwr_unavailable")
    elif acwr > _ACWR_SWEET_HIGH:
        r_acwr = _clamp((acwr - _ACWR_SWEET_HIGH) / (_ACWR_MAX_RISK_AT - _ACWR_SWEET_HIGH), 0.0, 1.0)
        if acwr > ACWR_HIGH_DEFAULT:
            reason_codes.append("acwr_above_threshold")
    elif acwr < _ACWR_SWEET_LOW:
        r_acwr = _clamp((_ACWR_SWEET_LOW - acwr) / _ACWR_SWEET_LOW, 0.0, 0.5)
        reason_codes.append("acwr_undertraining")
    else:
        r_acwr = 0.0

    r_asym: float | None = None
    if asymmetry_index is None:
        reason_codes.append("asymmetry_unavailable")
    else:
        r_asym = _clamp(
            (asymmetry_index - _ASYM_RISK_START) / (_ASYM_MAX_RISK_AT - _ASYM_RISK_START),
            0.0,
            1.0,
        )
        if asymmetry_index > ASYMMETRY_HIGH_DEFAULT:
            reason_codes.append("asymmetry_above_threshold")

    components: list[tuple[float, float]] = []
    if r_acwr is not None:
        components.append((_W_ACWR, r_acwr))
    if r_asym is not None:
        components.append((_W_ASYM, r_asym))

    score: float | None = None
    if components:
        total_weight = sum(w for w, _ in components)
        score = sum(w * r for w, r in components) / total_weight
        if len(components) < 2:
            reason_codes.append("partial_signal")
            score = min(score, _PARTIAL_SIGNAL_CAP)
        score = round(score, 3)
    else:
        reason_codes.append("no_signal")

    return {
        "injury_risk_score": score,
        "risk_acwr_component": _round_optional(r_acwr),
        "risk_asymmetry_component": _round_optional(r_asym),
        "reason_codes": sorted(set(reason_codes)),
        "model_variant": MODEL_VARIANT,
    }


def evaluate_workload_candidate(
    tracklet: dict[str, Any],
    *,
    fps: float,
    analytics_safe: bool,
    asymmetry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Per-clip workload-fusion review candidate for one tracklet.

    ``asymmetry`` is the optional per-tracklet output of
    ``gait_asymmetry.compute_gait_asymmetry`` (carried from the pose stage).
    ACWR needs multi-day history, so per-clip risk is asymmetry-only
    (``acwr_unavailable`` + ``partial_signal`` reason codes); the nightly
    rollup computes the fused score.
    """
    stats = sprint_stats(tracklet.get("track_points", []), fps)
    if stats is None:
        return None

    track_id = str(tracklet.get("id", "unknown"))
    identity_confidence = _confidence(
        tracklet, ("identity_confidence", "player_identity_confidence", "reid_confidence")
    )
    tracking_confidence = _confidence(tracklet, ("track_confidence", "tracking_confidence"))

    asymmetry_index = _number((asymmetry or {}).get("asymmetry_index"))
    risk = compute_injury_risk(None, asymmetry_index)
    reason_codes = list(risk["reason_codes"])

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
    if asymmetry is not None:
        asym_conf = _number(asymmetry.get("confidence"))
        if asym_conf is not None:
            confidence = min(confidence, asym_conf)

    return {
        "track_id": track_id,
        "player_id": str(player_id) if player_id is not None else None,
        "attribution": attribution,
        "position_group": (tracklet.get("position_group") or None),
        "sprint_count": stats["sprint_count"],
        "max_speed_yps": stats["max_speed_yps"],
        "distance_yards": stats["distance_yards"],
        "asymmetry_index": _round_optional(asymmetry_index),
        "injury_risk_score": risk["injury_risk_score"],
        "identity_confidence": _round_optional(identity_confidence),
        "tracking_confidence": _round_optional(tracking_confidence),
        "confidence": round(confidence, 3),
        "reason_codes": sorted(set(reason_codes)),
        "model_variant": MODEL_VARIANT,
        "experimental": True,
        "label": "experimental_workload_signal_not_diagnosis",
    }


def _interval_speeds(
    pts: list[dict[str, Any]], fps: float
) -> list[tuple[int, int, float]]:
    """Per-adjacent-point ``(start_frame, end_frame, speed_yps)`` intervals."""
    intervals: list[tuple[int, int, float]] = []
    for i in range(1, len(pts)):
        prev = pts[i - 1]
        cur = pts[i]
        f0 = int(prev.get("frame_number", 0) or 0)
        f1 = int(cur.get("frame_number", 0) or 0)
        dt_s = (f1 - f0) / max(fps, 1.0)
        if dt_s <= 0:
            continue
        x0 = _number(prev.get("field_x"))
        y0 = _number(prev.get("field_y"))
        x1 = _number(cur.get("field_x"))
        y1 = _number(cur.get("field_y"))
        if x0 is None or y0 is None or x1 is None or y1 is None:
            continue
        dist = math.hypot(x1 - x0, y1 - y0)
        intervals.append((f0, f1, dist / dt_s))
    return intervals


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
