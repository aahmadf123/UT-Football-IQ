"""Tests for regime-aware yard-line calibration (Issues #127, #138).

Three layers:
1. Pure-NumPy line clustering + correspondence building (no cv2).
2. Full single-frame detection on synthetic painted-field frames (cv2).
3. ``stage_calibrate`` regime branching with detection + backend stubbed out,
   so both FIXED_SIDELINE and DRONE_FOLLOW paths are exercised without video.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import stage_calibrate
from pipeline.homography import yardline_keypoints as yk
from pipeline.homography.confidence_scorer import (
    compute_confidence,
    parallel_line_score,
    temporal_stability_from_drift,
)
from pipeline.homography.field_template import default_template

# ── Field template ────────────────────────────────────────────────────────────


def test_field_template_landmarks_cover_yardlines_and_rows():
    tmpl = default_template()
    pts = tmpl.landmark_points()
    # 19 yard lines × 4 rows.
    assert len(pts) == len(tmpl.yard_lines_x) * 4
    assert tmpl.sideline_y_north == pytest.approx(26.665)
    assert tmpl.sideline_y_south == pytest.approx(-26.665)


# ── Confidence scoring ────────────────────────────────────────────────────────


def test_confidence_weights_sum_to_one():
    from pipeline.homography.confidence_scorer import WEIGHTS

    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_confidence_full_signal_is_high():
    bd = compute_confidence(
        inlier_ratio=1.0,
        line_count=20,
        parallel_line_score=1.0,
        temporal_stability=1.0,
        field_coverage=1.0,
    )
    assert bd.confidence == pytest.approx(1.0)


def test_confidence_low_inliers_drops_score():
    bd = compute_confidence(
        inlier_ratio=0.1,
        line_count=4,
        parallel_line_score=0.2,
        temporal_stability=0.3,
        field_coverage=0.4,
    )
    assert bd.confidence < 0.5


def test_parallel_line_score_perfectly_parallel():
    assert parallel_line_score([0.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_parallel_line_score_spread_drops():
    score = parallel_line_score([0.0, 0.3, -0.3])
    assert score < 0.5


def test_temporal_stability_monotonic():
    assert temporal_stability_from_drift(0.0) == pytest.approx(1.0)
    assert temporal_stability_from_drift(4.0) == pytest.approx(0.0)
    assert temporal_stability_from_drift(2.0) == pytest.approx(0.5)


# ── Line clustering + correspondences (pure NumPy) ────────────────────────────


def test_cluster_lines_by_angle_separates_orientations():
    # 3 vertical (theta≈0) + 2 horizontal (theta≈π/2).
    lines = [
        (100.0, 0.01), (200.0, 0.0), (300.0, 0.02),
        (50.0, math.pi / 2), (250.0, math.pi / 2 + 0.01),
    ]
    clusters = yk.cluster_lines_by_angle(lines)
    assert len(clusters) == 2
    # Largest cluster first → the 3 verticals.
    assert len(clusters[0]) == 3


def test_build_correspondences_from_synthetic_grid():
    # 4 vertical lines + 2 horizontal lines ⇒ 8 intersections.
    h, w = 360, 640
    verticals = [(float(x), 0.0) for x in (100, 250, 400, 550)]
    horizontals = [(80.0, math.pi / 2), (300.0, math.pi / 2)]
    result = yk.build_correspondences(verticals + horizontals, (h, w))
    assert result.has_enough()
    assert len(result.src_pts) == len(result.dst_pts)
    # Destination points must all be valid template coordinates.
    tmpl = default_template()
    valid_x = set(float(x) for x in tmpl.yard_lines_x)
    for x_yd, _ in result.dst_pts:
        assert x_yd in valid_x


def test_build_correspondences_insufficient_lines():
    result = yk.build_correspondences([(100.0, 0.0)], (360, 640))
    assert not result.has_enough()
    assert "insufficient_structured_lines" in result.reason_codes


# ── Full single-frame detection on a synthetic painted field (cv2) ────────────


def _synthetic_field_frame(h: int = 360, w: int = 640) -> np.ndarray:
    cv2 = pytest.importorskip("cv2")
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:] = (40, 180, 40)  # BGR green grass
    white = (255, 255, 255)
    for x in (120, 240, 360, 480):  # vertical yard lines
        cv2.line(frame, (x, 0), (x, h), white, 2)
    for y in (60, 300):  # sidelines
        cv2.line(frame, (0, y), (w, y), white, 2)
    return frame


def test_detect_keypoints_on_synthetic_frame():
    pytest.importorskip("cv2")
    frame = _synthetic_field_frame()
    result = yk.detect_keypoints(frame)
    assert result.field_coverage > 0.25
    assert result.has_enough(), result.reason_codes


def test_detect_keypoints_five_frames_yield_homography():
    """Acceptance: yard-line keypoints + DLT recover a homography on 5 frames."""
    pytest.importorskip("cv2")
    from pipeline.homography.dlt_ransac import ransac_homography

    successes = 0
    for _ in range(5):
        frame = _synthetic_field_frame()
        result = yk.detect_keypoints(frame)
        if not result.has_enough():
            continue
        H, inliers = ransac_homography(result.src_pts, result.dst_pts, threshold=3.0)
        if H is not None and inliers.sum() >= 4:
            successes += 1
    assert successes == 5


# ── stage_calibrate regime branching (detection + backend stubbed) ────────────


def _good_keypoints() -> yk.KeypointResult:
    """A clean coplanar correspondence set that fits cleanly under RANSAC."""
    H_true = np.array(
        [[1.1, 0.05, 30.0], [-0.04, 0.9, 12.0], [0.0003, 0.0001, 1.0]],
        dtype=np.float64,
    )
    xs, ys = np.meshgrid(np.linspace(5, 95, 4), np.linspace(-20, 20, 3))
    dst = np.column_stack([xs.ravel(), ys.ravel()]).astype(np.float64)
    homog = np.hstack([dst, np.ones((len(dst), 1))])
    proj = (H_true @ homog.T).T
    src = proj[:, :2] / proj[:, 2:3]
    return yk.KeypointResult(
        src_pts=src,
        dst_pts=dst,
        line_count=14,
        field_coverage=0.85,
        yardline_angles=[0.0, 0.001, 0.002, 0.0],
        reason_codes=[],
    )


@pytest.fixture()
def _stub_detection(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(yk, "detect_keypoints", lambda frame, template=None: _good_keypoints())

    def _capture(video_id, homography, confidence, **kwargs):
        captured.update(kwargs)
        captured["video_id"] = video_id
        captured["homography"] = homography
        captured["confidence"] = confidence
        return {"id": "stub"}

    monkeypatch.setattr(stage_calibrate.backend, "create_calibration", _capture)
    return captured


def test_fixed_sideline_produces_game_anchor(_stub_detection):
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    out = stage_calibrate._calibrate_fixed_sideline(
        "vid-1", "job-1", [frame, frame], regime="fixed_sideline"
    )
    assert out["is_game_anchor"] is True
    assert out["capture_regime"] == "fixed_sideline"
    assert out["analytics_safe"] is True
    assert _stub_detection["is_game_anchor"] is True
    assert _stub_detection["temporal_drift"] == 0.0
    assert _stub_detection["homography"] is not None
    assert "confidence_components" in _stub_detection["calibration_points"]


def test_drone_nightly_records_kalman_state(_stub_detection):
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    out = stage_calibrate._calibrate_drone(
        "vid-2", "job-2", [frame] * 4, regime="drone_follow",
        variant=stage_calibrate.VARIANT_KALMAN,
    )
    assert out["is_game_anchor"] is False
    assert out["capture_regime"] == "drone_follow"
    assert _stub_detection["kalman_state"] is not None
    assert len(_stub_detection["kalman_state"]) == 9


def test_drone_lite_variant_skips_kalman(_stub_detection):
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    stage_calibrate._calibrate_drone(
        "vid-3", "job-3", [frame] * 4, regime="drone_follow",
        variant=stage_calibrate.VARIANT_LITE,
    )
    assert _stub_detection["kalman_state"] is None


def test_no_frames_marks_unsafe(_stub_detection, monkeypatch):
    monkeypatch.setattr(stage_calibrate, "_sample_frames", lambda p, n: [])
    out = stage_calibrate._calibrate(
        "vid-4", __import__("pathlib").Path("/nope.mp4"), "job-4",
        regime="drone_follow", variant=stage_calibrate.VARIANT_KALMAN,
    )
    assert out["analytics_safe"] is False
    assert "no_frames" in out["reason_codes"]
