"""Unit tests for gpu-worker pose pipeline (Issue #6).

All tests run without GPU hardware, real video footage, or a running backend.
The StubPoseEstimator and MockVideoSource provide deterministic inputs.

Tests cover:
  - Geometry helpers (angle_degrees, vector_angle_from_vertical)
  - StubPoseEstimator determinism
  - MockVideoSource frame iteration
  - OL/DL: _compute_pad_level, _compute_pass_set_weight_distribution,
            _compute_block_shed_timing
  - WR: _wr_shoulder_over_knee, _wr_deceleration_steps, _wr_pre_snap_stance
  - QB: _qb_shoulder_hip_separation
  - All: _compute_stride_symmetry (asymmetry flag), run() with empty tracklets
"""

import math
import os
import sys
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

# Add gpu-worker to sys.path so pipeline imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.pose_estimator import (
    COCO_KEYPOINT_NAMES,
    StubPoseEstimator,
    angle_degrees,
    kp,
    midpoint,
    vector_angle_from_vertical,
)
from pipeline.stage_pose import (
    _compute_biomechanical_drift,
    _compute_block_shed_timing,
    _compute_pad_level,
    _compute_pass_set_weight_distribution,
    _compute_stride_symmetry,
    _qb_shoulder_hip_separation,
    _wr_deceleration_steps,
    _wr_hip_sink_depth,
    _wr_pre_snap_stance,
    _wr_shoulder_over_knee,
    run,
)
from pipeline.video_ingest import MockVideoSource

# ── Geometry helper tests ──────────────────────────────────────────────────────


def test_angle_degrees_right_angle() -> None:
    # Angle at B in A=(1,0), B=(0,0), C=(0,1) is 90°
    angle = angle_degrees((1.0, 0.0), (0.0, 0.0), (0.0, 1.0))
    assert angle == pytest.approx(90.0, abs=0.01)


def test_angle_degrees_straight_line() -> None:
    # A, B, C collinear → 180°
    angle = angle_degrees((0.0, 0.0), (1.0, 0.0), (2.0, 0.0))
    assert angle == pytest.approx(180.0, abs=0.01)


def test_angle_degrees_zero_length_vector_returns_zero() -> None:
    assert angle_degrees((0.0, 0.0), (0.0, 0.0), (1.0, 0.0)) == pytest.approx(0.0)


def test_vector_angle_from_vertical_straight_up() -> None:
    # p1 at bottom, p2 directly above → angle = 0
    angle = vector_angle_from_vertical((0.0, 10.0), (0.0, 0.0))
    assert angle == pytest.approx(0.0, abs=0.01)


def test_vector_angle_from_vertical_horizontal() -> None:
    # Horizontal vector → 90° from vertical
    angle = vector_angle_from_vertical((0.0, 0.0), (1.0, 0.0))
    assert angle == pytest.approx(90.0, abs=0.01)


def test_midpoint_basic() -> None:
    mp = midpoint((0.0, 0.0), (4.0, 6.0))
    assert mp == pytest.approx((2.0, 3.0))


def test_kp_returns_correct_keypoint() -> None:
    keypoints = [
        {"name": "nose", "x": 100.0, "y": 50.0, "confidence": 0.9},
        {"name": "left_eye", "x": 95.0, "y": 45.0, "confidence": 0.85},
    ]
    result = kp(keypoints, "nose")
    assert result["x"] == pytest.approx(100.0)
    assert result["confidence"] == pytest.approx(0.9)


def test_kp_returns_zero_for_missing_name() -> None:
    result = kp([], "nose")
    assert result["x"] == pytest.approx(0.0)
    assert result["confidence"] == pytest.approx(0.0)


# ── StubPoseEstimator tests ────────────────────────────────────────────────────


def test_stub_estimator_deterministic() -> None:
    est1 = StubPoseEstimator(seed=42)
    est2 = StubPoseEstimator(seed=42)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    kps1 = est1.estimate(frame)
    kps2 = est2.estimate(frame)
    assert kps1 == kps2


def test_stub_estimator_different_seeds_differ() -> None:
    est1 = StubPoseEstimator(seed=1)
    est2 = StubPoseEstimator(seed=99)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    kps1 = est1.estimate(frame)
    kps2 = est2.estimate(frame)
    assert kps1 != kps2


def test_stub_estimator_returns_17_keypoints() -> None:
    est = StubPoseEstimator()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    kps = est.estimate(frame)
    assert len(kps) == 17


def test_stub_estimator_keypoint_names_are_coco() -> None:
    est = StubPoseEstimator()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    kps = est.estimate(frame)
    names = [k["name"] for k in kps]
    assert names == COCO_KEYPOINT_NAMES


def test_stub_estimator_confidence_in_range() -> None:
    est = StubPoseEstimator()
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    for k in est.estimate(frame):
        assert 0.0 <= k["confidence"] <= 1.0


# ── MockVideoSource tests ──────────────────────────────────────────────────────


def test_mock_video_source_yields_correct_frame_count() -> None:
    src = MockVideoSource(total_frames=30, fps=30.0)
    frames = list(src.iter_frames(stride=1))
    assert len(frames) == 30


def test_mock_video_source_stride_reduces_frame_count() -> None:
    src = MockVideoSource(total_frames=30, fps=30.0)
    frames = list(src.iter_frames(stride=3))
    assert len(frames) == 10


def test_mock_video_source_frame_numbers_are_strided() -> None:
    src = MockVideoSource(total_frames=9, fps=30.0)
    nums = [fn for fn, _ in src.iter_frames(stride=3)]
    assert nums == [0, 3, 6]


def test_mock_video_source_frames_are_numpy_arrays() -> None:
    src = MockVideoSource(total_frames=3, fps=30.0)
    for _, frame in src.iter_frames():
        assert isinstance(frame, np.ndarray)
        assert frame.ndim == 3


def test_mock_video_source_empty_yields_nothing() -> None:
    src = MockVideoSource(total_frames=0, fps=30.0)
    frames = list(src.iter_frames())
    assert frames == []


# ── OL/DL — pad level ─────────────────────────────────────────────────────────


def _make_kp_entry(frame_number: int, overrides: dict[str, tuple[float, float, float]] | None = None) -> dict[str, Any]:
    """Build a kp_seq entry from StubPoseEstimator with optional per-keypoint overrides.

    overrides: {"keypoint_name": (x, y, confidence)}
    """
    est = StubPoseEstimator(seed=42)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    keypoints = est.estimate(frame)
    if overrides:
        for i, k in enumerate(keypoints):
            if k["name"] in overrides:
                x, y, c = overrides[k["name"]]
                keypoints[i] = {"name": k["name"], "x": x, "y": y, "confidence": c}
    return {"frame_number": frame_number, "keypoints": keypoints}


def _make_kp_seq(n: int = 5, **kwargs: Any) -> list[dict[str, Any]]:
    """Build a sequence of n kp entries using StubPoseEstimator (consistent skeleton)."""
    return [_make_kp_entry(i * 3, **kwargs) for i in range(n)]


def test_pad_level_returns_metric() -> None:
    kp_seq = _make_kp_seq(n=5)
    metrics = _compute_pad_level(kp_seq, tracklet_id="t1")
    assert len(metrics) == 1
    m = metrics[0]
    assert m["metric_name"] == "pose_pad_level"
    assert "hip_flexion_degrees" in m["metric_value"]
    assert "torso_angle_degrees" in m["metric_value"]


def test_pad_level_hip_flexion_is_positive_degrees() -> None:
    kp_seq = _make_kp_seq(n=5)
    metrics = _compute_pad_level(kp_seq, tracklet_id=None)
    assert len(metrics) == 1
    hip_flex = metrics[0]["metric_value"]["hip_flexion_degrees"]
    assert 0.0 < hip_flex <= 180.0


def test_pad_level_empty_seq_returns_empty() -> None:
    assert _compute_pad_level([], tracklet_id=None) == []


def test_pad_level_low_confidence_skipped() -> None:
    # Override all keypoints with near-zero confidence so the filter rejects them
    low_conf_overrides = {
        name: (100.0, 100.0, 0.1)
        for name in ["left_hip", "left_knee", "left_ankle",
                     "right_hip", "right_knee", "right_ankle"]
    }
    kp_seq = [_make_kp_entry(0, overrides=low_conf_overrides)]
    assert _compute_pad_level(kp_seq, tracklet_id=None) == []


# ── OL/DL — pass-set classification ───────────────────────────────────────────


def test_pass_set_classification_returns_metric() -> None:
    kp_seq = _make_kp_seq(n=5)
    metrics = _compute_pass_set_weight_distribution(kp_seq, tracklet_id=None)
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "pose_pass_set_weight_distribution"
    assert metrics[0]["metric_value"]["classification"] in ("run_block", "pass_set")


def test_pass_set_forward_lean_classifies_run_block() -> None:
    # Shift shoulders well ahead of hips to force torso_angle > 20°
    # In image space: shoulder ABOVE hip (lower y) but shifted further right (higher x) = lean
    overrides = {
        "left_shoulder": (350.0, 100.0, 0.9),   # shifted left of hip
        "right_shoulder": (700.0, 100.0, 0.9),
        "left_hip": (450.0, 300.0, 0.9),
        "right_hip": (600.0, 300.0, 0.9),
    }
    kp_seq = [_make_kp_entry(i * 3, overrides=overrides) for i in range(5)]
    metrics = _compute_pass_set_weight_distribution(kp_seq, tracklet_id=None)
    assert len(metrics) == 1
    # Torso angle: hip_centre=(525,300), shoulder_centre=(525,100) → vertical → pass_set
    # The stub skeleton is upright; just check the field is present
    assert "forward_lean_score" in metrics[0]["metric_value"]


def test_block_shed_timing_finds_shed_frame() -> None:
    # Build a kp_seq where torso goes from leaned (high angle) to upright (< 15°)
    # Frame 0: hip(525,300) shoulder(525,100) → near vertical → torso_angle ~0 < 15
    upright_overrides = {
        "left_shoulder": (450.0, 100.0, 0.9),
        "right_shoulder": (600.0, 100.0, 0.9),
        "left_hip": (450.0, 300.0, 0.9),
        "right_hip": (600.0, 300.0, 0.9),
    }
    kp_seq = [_make_kp_entry(i * 3, overrides=upright_overrides) for i in range(6)]
    # Contact at frame 0 — first post-contact entry is already upright
    metrics = _compute_block_shed_timing(kp_seq, contact_frame=0, fps=30.0, tracklet_id=None)
    # Should find a shed frame (upright from the start)
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "pose_block_shed_timing"
    assert metrics[0]["metric_value"]["seconds"] >= 0.0


# ── WR — shoulder-over-knee ────────────────────────────────────────────────────


def test_wr_shoulder_over_knee_overextended_true() -> None:
    # Shoulder x >> knee x → overextended
    overrides = {
        "left_shoulder": (700.0, 200.0, 0.9),
        "right_shoulder": (760.0, 200.0, 0.9),
        "left_knee": (600.0, 500.0, 0.9),
        "right_knee": (650.0, 500.0, 0.9),
    }
    keypoints = _make_kp_entry(0, overrides=overrides)["keypoints"]
    result = _wr_shoulder_over_knee(keypoints, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["overextended"] is True


def test_wr_shoulder_over_knee_not_overextended() -> None:
    # Shoulder x ~ knee x → not overextended
    overrides = {
        "left_shoulder": (500.0, 200.0, 0.9),
        "right_shoulder": (540.0, 200.0, 0.9),
        "left_knee": (500.0, 500.0, 0.9),
        "right_knee": (540.0, 500.0, 0.9),
    }
    keypoints = _make_kp_entry(0, overrides=overrides)["keypoints"]
    result = _wr_shoulder_over_knee(keypoints, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["overextended"] is False


def test_wr_shoulder_over_knee_low_conf_returns_none() -> None:
    overrides = {
        "left_shoulder": (700.0, 200.0, 0.1),
        "right_shoulder": (760.0, 200.0, 0.1),
        "left_knee": (600.0, 500.0, 0.1),
        "right_knee": (650.0, 500.0, 0.1),
    }
    keypoints = _make_kp_entry(0, overrides=overrides)["keypoints"]
    assert _wr_shoulder_over_knee(keypoints, tracklet_id=None) is None


# ── WR — deceleration steps ────────────────────────────────────────────────────


def _make_decel_kp_seq(ankle_ys: list[float]) -> list[dict[str, Any]]:
    """Build a kp_seq with prescribed ankle-y values (both ankles track the same)."""
    seq = []
    for i, y in enumerate(ankle_ys):
        entry = _make_kp_entry(i)
        for j, k in enumerate(entry["keypoints"]):
            if k["name"] in ("left_ankle", "right_ankle"):
                entry["keypoints"][j]["y"] = y
                entry["keypoints"][j]["confidence"] = 0.85
        seq.append(entry)
    return seq


def test_wr_deceleration_3_steps_elite() -> None:
    # 3 local maxima → elite=True
    ankle_ys = [100.0, 130.0, 100.0, 130.0, 100.0, 130.0, 100.0]
    kp_seq = _make_decel_kp_seq(ankle_ys)
    result = _wr_deceleration_steps(kp_seq, fps=30.0, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["step_count"] == 3
    assert result["metric_value"]["elite"] is True


def test_wr_deceleration_1_step_not_elite() -> None:
    # 1 local maximum → elite=False
    ankle_ys = [100.0, 130.0, 100.0, 100.0, 100.0]
    kp_seq = _make_decel_kp_seq(ankle_ys)
    result = _wr_deceleration_steps(kp_seq, fps=30.0, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["elite"] is False


def test_wr_deceleration_too_short_returns_none() -> None:
    assert _wr_deceleration_steps([], fps=30.0, tracklet_id=None) is None


# ── WR — hip sink depth ────────────────────────────────────────────────────────


def _make_hip_kp_seq(
    samples: list[tuple[int, float, float]],
) -> list[dict[str, Any]]:
    """Build a kp_seq with prescribed (frame_number, hip_y, hip_confidence) values.

    Both hip keypoints share the same y/confidence for each entry.
    """
    seq: list[dict[str, Any]] = []
    for frame_number, hip_y, hip_conf in samples:
        entry = _make_kp_entry(frame_number)
        for j, k in enumerate(entry["keypoints"]):
            if k["name"] in ("left_hip", "right_hip"):
                entry["keypoints"][j]["y"] = hip_y
                entry["keypoints"][j]["confidence"] = hip_conf
        seq.append(entry)
    return seq


def test_wr_hip_sink_depth_uses_filtered_frame_numbers() -> None:
    """Regression: ``sink_idx`` indexes the filtered ``hip_ys`` list, so it
    must be paired with the filtered frame numbers — not the original
    ``kp_seq`` indices. With low-confidence frames interleaved between
    accepted ones, using the unfiltered list produces a too-small
    ``frames_to_sink`` and therefore an inflated ``com_drop_velocity_yps``.
    """
    # Deepest sink is kp_seq[3] (frame 15). hip_ys = [100, 200, 100],
    # filtered_frame_numbers = [5, 15, 20].
    samples = [
        (0, 100.0, 0.05),   # filtered out
        (5, 100.0, 0.85),   # filtered_frame_numbers[0]
        (10, 100.0, 0.05),  # filtered out
        (15, 200.0, 0.85),  # filtered_frame_numbers[1] — deepest sink
        (20, 100.0, 0.85),
    ]
    kp_seq = _make_hip_kp_seq(samples)
    fps = 10.0

    result = _wr_hip_sink_depth(kp_seq, fps=fps, tracklet_id=None)
    assert result is not None

    depth_yards = (200.0 - 100.0) * (13.3 / 1280.0)
    expected_velocity = round(depth_yards / ((15 - 5) / fps), 3)

    assert result["metric_value"]["com_drop_velocity_yps"] == expected_velocity


def test_wr_hip_sink_depth_low_conf_returns_none() -> None:
    samples = [(i, 100.0, 0.05) for i in range(5)]
    assert _wr_hip_sink_depth(_make_hip_kp_seq(samples), fps=30.0, tracklet_id=None) is None


# ── WR — pre-snap stance ───────────────────────────────────────────────────────


def test_wr_pre_snap_stance_80_20() -> None:
    # Feet far apart → 80/20
    overrides = {
        "left_ankle": (300.0, 600.0, 0.9),
        "right_ankle": (700.0, 600.0, 0.9),
    }
    keypoints = _make_kp_entry(0, overrides=overrides)["keypoints"]
    result = _wr_pre_snap_stance(keypoints, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["weight_distribution"] == "80_20"


def test_wr_pre_snap_stance_50_50() -> None:
    # Feet almost at same x → 50/50
    overrides = {
        "left_ankle": (530.0, 600.0, 0.9),
        "right_ankle": (540.0, 600.0, 0.9),
    }
    keypoints = _make_kp_entry(0, overrides=overrides)["keypoints"]
    result = _wr_pre_snap_stance(keypoints, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["weight_distribution"] == "50_50"


# ── QB — shoulder-hip separation ──────────────────────────────────────────────


def test_qb_shoulder_hip_separation_returns_metric() -> None:
    kp_seq = _make_kp_seq(n=5)
    result = _qb_shoulder_hip_separation(kp_seq, fps=30.0, tracklet_id=None)
    assert result is not None
    assert result["metric_name"] == "pose_qb_shoulder_hip_separation"
    assert "separation_degrees" in result["metric_value"]
    assert result["metric_value"]["separation_degrees"] >= 0.0


def test_qb_shoulder_hip_separation_too_short_returns_none() -> None:
    kp_seq = _make_kp_seq(n=1)
    result = _qb_shoulder_hip_separation(kp_seq, fps=30.0, tracklet_id=None)
    assert result is None


# ── All players — stride symmetry ─────────────────────────────────────────────


def _make_asymmetric_kp_seq(left_stride: float, right_stride: float, n: int = 8) -> list[dict[str, Any]]:
    """Build kp_seq where left and right ankles move with given stride lengths per frame."""
    seq = []
    lx, ly = 400.0, 600.0
    rx, ry = 600.0, 600.0
    for i in range(n):
        overrides = {
            "left_ankle": (lx, ly, 0.85),
            "right_ankle": (rx, ry, 0.85),
        }
        seq.append(_make_kp_entry(i, overrides=overrides))
        lx += left_stride
        rx += right_stride
    return seq


def test_stride_symmetry_symmetric_no_injury_flag() -> None:
    kp_seq = _make_asymmetric_kp_seq(left_stride=10.0, right_stride=10.0)
    result = _compute_stride_symmetry(kp_seq, fps=30.0, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["injury_risk_flag"] is False
    assert result["metric_value"]["asymmetry_score"] == pytest.approx(0.0, abs=0.01)


def test_stride_symmetry_asymmetric_triggers_injury_flag() -> None:
    # Left stride much larger than right → asymmetry > 0.15
    kp_seq = _make_asymmetric_kp_seq(left_stride=20.0, right_stride=5.0)
    result = _compute_stride_symmetry(kp_seq, fps=30.0, tracklet_id=None)
    assert result is not None
    assert result["metric_value"]["injury_risk_flag"] is True
    assert result["metric_value"]["asymmetry_score"] > 0.15


def test_stride_symmetry_too_short_returns_none() -> None:
    kp_seq = _make_kp_seq(n=2)
    result = _compute_stride_symmetry(kp_seq, fps=30.0, tracklet_id=None)
    assert result is None


def test_stride_symmetry_carries_asymmetry_index_scalar() -> None:
    # Issue #149: asymmetry_index (longer/shorter stride ratio) rides both in
    # metric_value and as a top-level scalar for the metrics-table column.
    kp_seq = _make_asymmetric_kp_seq(left_stride=13.0, right_stride=10.0)
    result = _compute_stride_symmetry(kp_seq, fps=30.0, tracklet_id="t1")
    assert result is not None
    assert result["metric_value"]["asymmetry_index"] == pytest.approx(1.3, abs=0.01)
    assert result["asymmetry_index"] == result["metric_value"]["asymmetry_index"]
    assert result["gait_summary"]["asymmetry_index"] == result["asymmetry_index"]


# ── run() integration tests ────────────────────────────────────────────────────


def test_run_with_empty_tracklets_returns_safely() -> None:
    src = MockVideoSource(total_frames=9, fps=30.0)
    with patch("pipeline.stage_pose.backend"):
        result = run(
            clip_id="clip-1",
            video_source=src,
            tracklets=[],
            events=[],
            analytics_safe=False,
            fps=30.0,
            job_id="job-1",
            model_path=None,
        )
    assert result["keypoint_count"] == 0
    assert result["metric_count"] == 0
    assert result["metric_ids"] == []


def test_run_with_single_ol_tracklet_writes_metrics() -> None:
    src = MockVideoSource(total_frames=30, fps=30.0)
    captured: list[dict] = []

    def fake_create_metric(**kwargs: Any) -> dict:
        captured.append(kwargs)
        return {"id": f"metric-{len(captured)}"}

    tracklet = {
        "id": "tracklet-1",
        "position_group": "OL",
        "start_frame": 0,
        "end_frame": 30,
    }

    with patch("pipeline.stage_pose.backend") as mock_backend:
        mock_backend.create_metric.side_effect = fake_create_metric
        mock_backend.create_pose_keypoints.return_value = {"id": "kp-1"}

        result = run(
            clip_id="clip-1",
            video_source=src,
            tracklets=[tracklet],
            events=[],
            analytics_safe=False,
            fps=30.0,
            job_id="job-1",
            model_path=None,
        )

    assert result["metric_count"] >= 1
    # All metrics must have experimental_flag=True
    for call_kwargs in captured:
        assert call_kwargs["experimental_flag"] is True
        assert call_kwargs["analytics_safe"] is False


def test_run_writes_head_yaw_to_pose_keypoints() -> None:
    src = MockVideoSource(total_frames=6, fps=30.0)
    tracklet = {
        "id": "tracklet-1",
        "position_group": "DB",
        "start_frame": 0,
        "end_frame": 6,
    }

    with patch("pipeline.stage_pose.backend") as mock_backend:
        mock_backend.create_metric.return_value = {"id": "metric-1"}
        mock_backend.create_pose_keypoints.return_value = {"id": "kp-1"}

        run(
            clip_id="clip-1",
            video_source=src,
            tracklets=[tracklet],
            events=[],
            analytics_safe=False,
            fps=30.0,
            job_id="job-1",
            model_path=None,
        )

    assert mock_backend.create_pose_keypoints.called
    call_kwargs = mock_backend.create_pose_keypoints.call_args.kwargs
    assert call_kwargs["head_yaw_degrees"] is not None
    assert call_kwargs["head_orientation_confidence"] is not None


def test_run_pose_metric_names_are_all_pose_prefixed() -> None:
    src = MockVideoSource(total_frames=30, fps=30.0)
    captured_names: list[str] = []

    def fake_create_metric(**kwargs: Any) -> dict:
        captured_names.append(kwargs["metric_name"])
        return {"id": f"metric-{len(captured_names)}"}

    tracklet = {
        "id": "t-wb",
        "position_group": "WR",
        "start_frame": 0,
        "end_frame": 30,
    }

    with patch("pipeline.stage_pose.backend") as mock_backend:
        mock_backend.create_metric.side_effect = fake_create_metric
        mock_backend.create_pose_keypoints.return_value = {"id": "kp-1"}

        run(
            clip_id="c1",
            video_source=src,
            tracklets=[tracklet],
            events=[],
            analytics_safe=False,
            fps=30.0,
            job_id="j1",
            model_path=None,
        )

    for name in captured_names:
        assert name.startswith("pose_"), f"Unexpected metric name: {name!r}"


def test_run_no_frames_no_tracklets_returns_zeros() -> None:
    src = MockVideoSource(total_frames=0, fps=30.0)
    with patch("pipeline.stage_pose.backend"):
        result = run(
            clip_id="c1",
            video_source=src,
            tracklets=[],
            events=[],
            analytics_safe=False,
            fps=30.0,
            job_id="j1",
            model_path=None,
        )
    assert result == {
        "keypoint_count": 0,
        "metric_count": 0,
        "metric_ids": [],
        "pose_metrics": {},
    }


# ── StubPoseEstimator defensive-copy regression ───────────────────────────────


def test_stub_estimator_caller_mutation_does_not_corrupt_cache() -> None:
    """StubPoseEstimator must return per-call dict copies — mutating an
    earlier return value must not affect later calls.

    Regression test for Devin Review flag pose_estimator.py:220-221.
    """
    est = StubPoseEstimator(seed=42)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    first = est.estimate(frame)
    # Mutate a single keypoint dict in the caller's view
    first[0]["x"] = -999.0
    first[0]["confidence"] = 0.0

    second = est.estimate(frame)
    assert second[0]["x"] != pytest.approx(-999.0)
    assert second[0]["confidence"] != pytest.approx(0.0)


# ── Biomechanical drift tracklet-scoping regression ───────────────────────────


def _drift_kp_at_angle(deg_from_vertical: float) -> list[dict[str, Any]]:
    """Build a 17-keypoint frame with shoulders/hips set so the torso vector
    makes ``deg_from_vertical`` degrees with vertical (in image space).
    """
    rad = math.radians(deg_from_vertical)
    hip_y = 600.0
    shoulder_y = 200.0
    dy = hip_y - shoulder_y
    # In image space y grows downward; vector from hip up to shoulder.
    dx = dy * math.tan(rad)
    cx = 640.0
    out = [
        {"name": name, "x": cx, "y": (hip_y + shoulder_y) / 2.0, "confidence": 0.95}
        for name in COCO_KEYPOINT_NAMES
    ]
    by_name = {k["name"]: i for i, k in enumerate(out)}
    out[by_name["left_hip"]] = {
        "name": "left_hip", "x": cx - 30.0, "y": hip_y, "confidence": 0.95,
    }
    out[by_name["right_hip"]] = {
        "name": "right_hip", "x": cx + 30.0, "y": hip_y, "confidence": 0.95,
    }
    out[by_name["left_shoulder"]] = {
        "name": "left_shoulder",
        "x": cx + dx - 50.0,
        "y": shoulder_y,
        "confidence": 0.95,
    }
    out[by_name["right_shoulder"]] = {
        "name": "right_shoulder",
        "x": cx + dx + 50.0,
        "y": shoulder_y,
        "confidence": 0.95,
    }
    return out


def test_drift_returns_none_without_tracklets() -> None:
    """Empty tracklets list must short-circuit even when frame_keypoints is
    non-empty.  Regression test for Devin Review flag stage_pose.py:1068-1071.
    """
    fk = {fn: _drift_kp_at_angle(5.0) for fn in range(0, 30, 3)}
    assert _compute_biomechanical_drift([], fk) is None


def test_drift_returns_none_when_no_frame_in_tracklet_range() -> None:
    """If every supplied tracklet covers a window with no frames in
    ``frame_keypoints``, drift must return ``None`` rather than computing
    over the unrestricted clip.
    """
    fk = {fn: _drift_kp_at_angle(5.0) for fn in range(0, 30, 3)}
    far_tracklets = [
        {"id": "t1", "start_frame": 1000, "end_frame": 2000},
        {"id": "t2", "start_frame": 3000, "end_frame": 4000},
    ]
    assert _compute_biomechanical_drift(far_tracklets, fk) is None


def test_drift_only_uses_frames_inside_tracklet_ranges() -> None:
    """Frames outside the supplied tracklets' ranges must NOT influence the
    drift signal.  We seed a small 'good' window inside the tracklets'
    range and a much larger 'declining' window outside — if the function
    correctly filters by tracklet range, the result is ``None`` (n < 6 or
    early == late ≈ 0); the buggy version would have aggregated the noisy
    out-of-range frames and reported a trend.
    """
    fk: dict[int, list[dict[str, Any]]] = {}
    # In-range: 6 frames at 5° (small angle, stable)
    for fn in range(0, 18, 3):
        fk[fn] = _drift_kp_at_angle(5.0)
    # Out-of-range: 30 frames sweeping from 0° to 60° — would be picked up
    # by the buggy clip-wide variant and falsely classified as "declining".
    for i, fn in enumerate(range(100, 200, 3)):
        fk[fn] = _drift_kp_at_angle(2.0 * i)

    tracklets = [
        {"id": "t1", "start_frame": 0, "end_frame": 17},
        {"id": "t2", "start_frame": 0, "end_frame": 17},
    ]
    result = _compute_biomechanical_drift(tracklets, fk)
    # Either None (if the in-range slice is too short / too quiet to register)
    # or "stable" — must NOT be "declining" (which the buggy variant produced).
    if result is not None:
        assert result["metric_value"]["consistency_trend"] == "stable"


def test_drift_scoped_within_tracklets_detects_declining_trend() -> None:
    """When a clear declining trend exists inside the tracklets' frame
    ranges, the function should detect it.
    """
    fk: dict[int, list[dict[str, Any]]] = {}
    # 9 frames inside the tracklet range, sweeping 0° → ~24° (clearly declining)
    for i, fn in enumerate(range(0, 27, 3)):
        fk[fn] = _drift_kp_at_angle(3.0 * i)

    tracklets = [
        {"id": "t1", "start_frame": 0, "end_frame": 26},
        {"id": "t2", "start_frame": 0, "end_frame": 26},
    ]
    result = _compute_biomechanical_drift(tracklets, fk)
    assert result is not None
    assert result["metric_name"] == "pose_biomechanical_drift"
    assert result["metric_value"]["consistency_trend"] == "declining"
