"""Tests for effort review candidate scoring (Issue #142)."""

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.metrics.effort_zscore import evaluate_effort_candidate
from pipeline.stage_metrics import _effort_metrics


def _tracklet(track_id: str, speeds: list[float], **overrides: Any) -> dict[str, Any]:
    points = [{"frame_number": 0, "field_x": 0.0, "field_y": 0.0}]
    x = 0.0
    for frame, speed in enumerate(speeds, start=1):
        x += speed / 10.0
        points.append({"frame_number": frame, "field_x": x, "field_y": 0.0})
    base = {
        "id": track_id,
        "player_id": f"player-{track_id}",
        "position": "WR",
        "position_group": "Skill",
        "track_confidence": 0.92,
        "identity_confidence": 0.91,
        "baseline_max_speed_mean_yps": 10.0,
        "baseline_max_speed_std_yps": 2.0,
        "baseline_avg_speed_yps": 8.0,
        "track_points": points,
    }
    base.update(overrides)
    return base


def test_effort_zscore_flags_known_jog_clip() -> None:
    candidate = evaluate_effort_candidate(
        _tracklet("jog", [3.0] * 8),
        snap_frame=0,
        fps=10.0,
        analytics_safe=True,
    )

    assert candidate is not None
    assert candidate["effort_zscore"] == pytest.approx(-3.5)
    assert candidate["loaf_flag"] is True
    assert "possible_effort_drop" in candidate["reason_codes"]
    assert candidate["review_state"] == "needs_coach_review"


def test_effort_zscore_does_not_flag_full_effort_clip() -> None:
    candidate = evaluate_effort_candidate(
        _tracklet("full", [9.0, 10.0, 11.0, 11.5, 10.5, 9.5]),
        snap_frame=0,
        fps=10.0,
        analytics_safe=True,
    )

    assert candidate is not None
    assert candidate["effort_zscore"] == pytest.approx(0.75)
    assert candidate["loaf_flag"] is False
    assert "possible_effort_drop" not in candidate["reason_codes"]


def test_low_identity_stays_anonymous_for_review() -> None:
    candidate = evaluate_effort_candidate(
        _tracklet("anon", [3.0] * 8, identity_confidence=0.4),
        snap_frame=0,
        fps=10.0,
        analytics_safe=True,
    )

    assert candidate is not None
    assert candidate["player_id"] is None
    assert candidate["attribution"] == "anonymous_track"
    assert "low_identity_confidence" in candidate["reason_codes"]


def test_stage_metrics_wires_effort_review_candidate() -> None:
    metrics = _effort_metrics(
        [_tracklet("wired", [3.0] * 8)],
        events=[],
        snap_frame=0,
        fps=10.0,
        suppressed=False,
        reason=None,
    )

    candidates = [m for m in metrics if m["metric_name"] == "effort_review_candidate"]
    assert len(candidates) == 1
    assert candidates[0]["effort_zscore"] == pytest.approx(-3.5)
    assert candidates[0]["loaf_flag"] is True
    assert candidates[0]["experimental_flag"] is True

