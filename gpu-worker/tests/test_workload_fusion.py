"""Tests for workload-fusion injury-risk signals (Issue #149)."""

import datetime as dt
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.metrics.workload_fusion import (
    ACWR_HIGH_DEFAULT,
    ASYMMETRY_HIGH_DEFAULT,
    MODEL_VARIANT,
    compute_acwr,
    compute_injury_risk,
    evaluate_workload_candidate,
    sprint_stats,
)
from pipeline.stage_metrics import _workload_metrics


def _points(speeds_yps: list[float], fps: float = 10.0) -> list[dict[str, Any]]:
    """Track points where interval i has the given speed (yd/s) at ``fps``."""
    points = [{"frame_number": 0, "field_x": 0.0, "field_y": 0.0}]
    x = 0.0
    for frame, speed in enumerate(speeds_yps, start=1):
        x += speed / fps
        points.append({"frame_number": frame, "field_x": x, "field_y": 0.0})
    return points


def _tracklet(track_id: str, speeds: list[float], **overrides: Any) -> dict[str, Any]:
    base = {
        "id": track_id,
        "player_id": f"player-{track_id}",
        "position_group": "WR",
        "track_confidence": 0.92,
        "identity_confidence": 0.91,
        "track_points": _points(speeds),
    }
    base.update(overrides)
    return base


# ── sprint_stats ──────────────────────────────────────────────────────────────


def test_sprint_stats_counts_sustained_high_speed_run() -> None:
    # 6 intervals at 8 yd/s and fps=10 → 0.6 s ≥ 0.5 s minimum → one sprint.
    stats = sprint_stats(_points([8.0] * 6), fps=10.0)

    assert stats is not None
    assert stats["sprint_count"] == 1
    assert stats["max_speed_yps"] == pytest.approx(8.0)


def test_sprint_stats_ignores_too_short_burst() -> None:
    # 0.4 s above threshold does not count as a sprint.
    stats = sprint_stats(_points([2.0, 8.0, 8.0, 8.0, 8.0, 2.0]), fps=10.0)

    assert stats is not None
    assert stats["sprint_count"] == 0


def test_sprint_stats_ignores_sub_threshold_speeds() -> None:
    stats = sprint_stats(_points([5.0] * 10), fps=10.0)

    assert stats is not None
    assert stats["sprint_count"] == 0
    assert stats["max_speed_yps"] == pytest.approx(5.0)


def test_sprint_stats_counts_separated_sprints() -> None:
    stats = sprint_stats(_points([8.0] * 6 + [2.0] * 3 + [9.0] * 6), fps=10.0)

    assert stats is not None
    assert stats["sprint_count"] == 2
    assert stats["max_speed_yps"] == pytest.approx(9.0)


def test_sprint_stats_distance_sums_intervals() -> None:
    stats = sprint_stats(_points([5.0] * 4), fps=10.0)

    assert stats is not None
    assert stats["distance_yards"] == pytest.approx(2.0)


def test_sprint_stats_no_field_coords_returns_none() -> None:
    pts = [{"frame_number": i, "field_x": None, "field_y": None} for i in range(5)]
    assert sprint_stats(pts, fps=10.0) is None


# ── compute_acwr ──────────────────────────────────────────────────────────────


def _daily_loads(loads_by_offset: dict[int, float], as_of: dt.date) -> dict[dt.date, float]:
    return {as_of - dt.timedelta(days=offset): load for offset, load in loads_by_offset.items()}


def test_acwr_steady_load_is_one() -> None:
    as_of = dt.date(2026, 7, 1)
    loads = _daily_loads({i: 100.0 for i in range(28)}, as_of)

    result = compute_acwr(loads, as_of)

    assert result["acwr"] == pytest.approx(1.0)
    assert result["acute_load_7d"] == pytest.approx(100.0)
    assert result["chronic_load_28d"] == pytest.approx(100.0)
    assert result["reason_codes"] == []


def test_acwr_acute_spike_reads_high() -> None:
    as_of = dt.date(2026, 7, 1)
    loads = _daily_loads(
        {**{i: 200.0 for i in range(7)}, **{i: 100.0 for i in range(7, 28)}}, as_of
    )

    result = compute_acwr(loads, as_of)

    assert result["acwr"] == pytest.approx(1.6)


def test_acwr_insufficient_history_returns_none() -> None:
    as_of = dt.date(2026, 7, 1)
    loads = _daily_loads({i: 150.0 for i in range(5)}, as_of)

    result = compute_acwr(loads, as_of)

    assert result["acwr"] is None
    assert "insufficient_chronic_history" in result["reason_codes"]


def test_acwr_accepts_iso_string_dates() -> None:
    as_of = "2026-07-01"
    day = dt.date(2026, 7, 1)
    loads = {
        (day - dt.timedelta(days=i)).isoformat(): 100.0 for i in range(28)
    }

    result = compute_acwr(loads, as_of)

    assert result["acwr"] == pytest.approx(1.0)


def test_acwr_rejects_bad_as_of() -> None:
    with pytest.raises(ValueError):
        compute_acwr({}, "not-a-date")


# ── compute_injury_risk ───────────────────────────────────────────────────────


def test_risk_zero_in_acwr_sweet_spot_with_symmetric_gait() -> None:
    result = compute_injury_risk(acwr=1.0, asymmetry_index=1.0)

    assert result["injury_risk_score"] == pytest.approx(0.0)
    assert result["model_variant"] == MODEL_VARIANT


def test_risk_maximal_when_both_signals_extreme() -> None:
    result = compute_injury_risk(acwr=2.0, asymmetry_index=1.5)

    assert result["injury_risk_score"] == pytest.approx(1.0)
    assert "acwr_above_threshold" in result["reason_codes"]
    assert "asymmetry_above_threshold" in result["reason_codes"]


def test_risk_threshold_boundaries_are_strict() -> None:
    # Exactly at the alert thresholds (1.5 / 1.3) is NOT above them.
    result = compute_injury_risk(acwr=ACWR_HIGH_DEFAULT, asymmetry_index=ASYMMETRY_HIGH_DEFAULT)

    assert "acwr_above_threshold" not in result["reason_codes"]
    assert "asymmetry_above_threshold" not in result["reason_codes"]
    assert result["injury_risk_score"] is not None
    assert 0.0 < result["injury_risk_score"] < 1.0


def test_risk_partial_signal_is_capped() -> None:
    result = compute_injury_risk(acwr=None, asymmetry_index=1.5)

    assert result["injury_risk_score"] == pytest.approx(0.6)
    assert "partial_signal" in result["reason_codes"]
    assert "acwr_unavailable" in result["reason_codes"]


def test_risk_undertraining_is_moderate_at_most() -> None:
    result = compute_injury_risk(acwr=0.3, asymmetry_index=1.0)

    assert "acwr_undertraining" in result["reason_codes"]
    assert result["injury_risk_score"] is not None
    assert result["injury_risk_score"] <= 0.5


def test_risk_no_signal_returns_none() -> None:
    result = compute_injury_risk(acwr=None, asymmetry_index=None)

    assert result["injury_risk_score"] is None
    assert "no_signal" in result["reason_codes"]


# ── evaluate_workload_candidate ───────────────────────────────────────────────


def test_candidate_attributes_confident_identity_to_player() -> None:
    candidate = evaluate_workload_candidate(
        _tracklet("fast", [8.0] * 6),
        fps=10.0,
        analytics_safe=True,
        asymmetry={"asymmetry_index": 1.2, "confidence": 0.8},
    )

    assert candidate is not None
    assert candidate["player_id"] == "player-fast"
    assert candidate["attribution"] == "player"
    assert candidate["sprint_count"] == 1
    assert candidate["asymmetry_index"] == pytest.approx(1.2)
    assert candidate["injury_risk_score"] is not None
    assert candidate["experimental"] is True
    assert candidate["label"] == "experimental_workload_signal_not_diagnosis"


def test_candidate_low_identity_stays_anonymous() -> None:
    candidate = evaluate_workload_candidate(
        _tracklet("anon", [8.0] * 6, identity_confidence=0.4),
        fps=10.0,
        analytics_safe=True,
    )

    assert candidate is not None
    assert candidate["player_id"] is None
    assert candidate["attribution"] == "anonymous_track"
    assert "low_identity_confidence" in candidate["reason_codes"]


def test_candidate_without_pose_flags_asymmetry_unavailable() -> None:
    candidate = evaluate_workload_candidate(
        _tracklet("nopose", [8.0] * 6),
        fps=10.0,
        analytics_safe=True,
    )

    assert candidate is not None
    assert candidate["asymmetry_index"] is None
    assert "asymmetry_unavailable" in candidate["reason_codes"]


def test_candidate_unsafe_calibration_flagged() -> None:
    candidate = evaluate_workload_candidate(
        _tracklet("unsafe", [8.0] * 6),
        fps=10.0,
        analytics_safe=False,
    )

    assert candidate is not None
    assert "calibration_not_safe" in candidate["reason_codes"]


# ── stage_metrics wiring ──────────────────────────────────────────────────────


def test_stage_metrics_wires_workload_fusion() -> None:
    tracklet = _tracklet("wired", [8.0] * 6)
    pose_metrics = {"wired": {"asymmetry_index": 1.4, "confidence": 0.8}}

    metrics = _workload_metrics(
        [tracklet],
        fps=10.0,
        analytics_safe=True,
        pose_metrics=pose_metrics,
        suppressed=False,
        reason=None,
    )

    assert len(metrics) == 1
    m = metrics[0]
    assert m["metric_name"] == "workload_fusion"
    assert m["experimental_flag"] is True
    assert m["sprint_count"] == 1
    assert m["asymmetry_index"] == pytest.approx(1.4)
    assert m["injury_risk_score"] is not None
    assert "asymmetry_above_threshold" in m["metric_value"]["reason_codes"]


def test_stage_metrics_workload_without_pose_metrics() -> None:
    metrics = _workload_metrics(
        [_tracklet("solo", [8.0] * 6)],
        fps=10.0,
        analytics_safe=True,
        pose_metrics=None,
        suppressed=False,
        reason=None,
    )

    assert len(metrics) == 1
    assert metrics[0]["asymmetry_index"] is None
    assert "asymmetry_unavailable" in metrics[0]["metric_value"]["reason_codes"]
