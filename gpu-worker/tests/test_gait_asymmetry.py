"""Tests for pose-based gait asymmetry (Issue #149)."""

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.metrics.gait_asymmetry import compute_gait_asymmetry


def _ankle_entry(
    frame_number: int,
    left: tuple[float, float],
    right: tuple[float, float],
    confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "frame_number": frame_number,
        "keypoints": [
            {"name": "left_ankle", "x": left[0], "y": left[1], "confidence": confidence},
            {"name": "right_ankle", "x": right[0], "y": right[1], "confidence": confidence},
        ],
    }


def _walking_seq(
    n: int,
    left_step: float,
    right_step: float,
    confidence: float = 0.9,
) -> list[dict[str, Any]]:
    return [
        _ankle_entry(
            i * 3,
            left=(100.0 + i * left_step, 400.0),
            right=(120.0 + i * right_step, 400.0),
            confidence=confidence,
        )
        for i in range(n)
    ]


def test_symmetric_gait_has_index_near_one() -> None:
    result = compute_gait_asymmetry(_walking_seq(8, left_step=10.0, right_step=10.0))

    assert result is not None
    assert result["asymmetry_index"] == pytest.approx(1.0)
    assert result["left_right_ratio"] == pytest.approx(1.0)
    assert result["asymmetry_score"] == pytest.approx(0.0)


def test_thirty_percent_longer_left_stride_reads_one_point_three() -> None:
    result = compute_gait_asymmetry(_walking_seq(8, left_step=13.0, right_step=10.0))

    assert result is not None
    assert result["asymmetry_index"] == pytest.approx(1.3)
    assert result["left_right_ratio"] == pytest.approx(1.3)
    assert result["left_stride_mean"] > result["right_stride_mean"]


def test_shorter_left_stride_still_reads_above_one() -> None:
    # asymmetry_index is side-agnostic: longer/shorter, whichever side.
    result = compute_gait_asymmetry(_walking_seq(8, left_step=10.0, right_step=13.0))

    assert result is not None
    assert result["asymmetry_index"] == pytest.approx(1.3)
    assert result["left_right_ratio"] == pytest.approx(10.0 / 13.0, abs=1e-3)


def test_low_confidence_frames_are_dropped() -> None:
    seq = _walking_seq(8, left_step=10.0, right_step=10.0)
    # A garbage low-confidence detour must not disturb the symmetric result.
    seq.insert(4, _ankle_entry(11, left=(9999.0, 9999.0), right=(0.0, 0.0), confidence=0.1))

    result = compute_gait_asymmetry(seq)

    assert result is not None
    assert result["asymmetry_index"] == pytest.approx(1.0)


def test_all_low_confidence_returns_none() -> None:
    assert compute_gait_asymmetry(_walking_seq(8, 10.0, 10.0, confidence=0.1)) is None


def test_too_few_samples_returns_none() -> None:
    assert compute_gait_asymmetry(_walking_seq(3, 10.0, 10.0)) is None


def test_stationary_player_returns_none() -> None:
    assert compute_gait_asymmetry(_walking_seq(8, 0.0, 0.0)) is None
