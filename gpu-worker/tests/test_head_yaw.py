"""Tests for pose-derived body-orientation proxy (Issue #143)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.pose.head_yaw import (
    classify_body_orientation_proxy,
    compute_head_yaw,
    summarize_body_orientation,
)


def _keypoints(nose_x: float, nose_y: float = 1.0, confidence: float = 0.9) -> list[dict]:
    return [
        {"name": "nose", "x": nose_x, "y": nose_y, "confidence": confidence},
        {"name": "left_ear", "x": -1.0, "y": 0.0, "confidence": confidence},
        {"name": "right_ear", "x": 1.0, "y": 0.0, "confidence": confidence},
        {"name": "left_shoulder", "x": -20.0, "y": 20.0, "confidence": confidence},
        {"name": "right_shoulder", "x": 20.0, "y": 20.0, "confidence": confidence},
    ]


def test_compute_head_yaw_uses_nose_to_ear_midpoint_formula() -> None:
    result = compute_head_yaw(_keypoints(nose_x=1.0, nose_y=1.0))

    assert result is not None
    assert result["head_yaw_degrees"] == pytest.approx(45.0)
    assert result["orientation_class"] == "body_on_receiver"
    assert result["confidence"] == pytest.approx(0.9)


def test_head_yaw_requires_confident_pose_points() -> None:
    assert compute_head_yaw(_keypoints(nose_x=1.0, confidence=0.1)) is None


def test_classification_uses_proxy_language() -> None:
    assert classify_body_orientation_proxy(-50.0) == "body_inside"
    assert classify_body_orientation_proxy(50.0) == "body_on_receiver"
    assert classify_body_orientation_proxy(10.0) == "body_backfield"


def test_summarize_body_orientation_returns_review_state() -> None:
    summary = summarize_body_orientation([
        {"frame_number": 0, "keypoints": _keypoints(nose_x=1.0)},
        {"frame_number": 1, "keypoints": _keypoints(nose_x=1.0)},
    ])

    assert summary is not None
    assert summary["orientation_class"] == "body_on_receiver"
    assert summary["frame_count"] == 2
    assert summary["review_state"] == "needs_coach_review"

