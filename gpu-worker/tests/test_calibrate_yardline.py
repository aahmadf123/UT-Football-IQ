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
from pipeline.homography import project
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
    """A clean coplanar correspondence set that fits cleanly under RANSAC.

    ``H_true`` maps field yards → pixels, and is scaled so the correspondences
    span a 640×360 frame the way a real camera's would. That matters: an earlier
    version put the whole field inside a 100×30 pixel patch, which RANSAC fits
    perfectly well but which no camera could produce. The stage now rejects a
    homography that places the middle of the frame off the planet, so a fixture
    has to be geometrically coherent, not merely self-consistent.
    """
    H_true = np.array(
        [[5.8, 0.2, 30.0], [-0.15, 6.0, 180.0], [0.0003, 0.0001, 1.0]],
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


# ── Orientation-agnostic family assignment ────────────────────────────────────


def _rot(lines, degrees, centre=(320.0, 180.0)):
    """Rotate a set of ``(rho, theta)`` lines about a point.

    About the *frame centre*, not the origin: rotating a 640×360 grid about its
    top-left corner swings most of it out of frame, and the detector drops
    crossings that land outside — so such a fixture would test the margin filter
    rather than the orientation handling.

    For a rotation R about ``c``, the normal turns with the line and the offset
    picks up the movement of ``c`` along it: ``rho' = rho - n·c + (Rn)·c``.
    """
    r = math.radians(degrees)
    cx, cy = centre
    out = []
    for rho, theta in lines:
        nx, ny = math.cos(theta), math.sin(theta)
        t2 = (theta + r) % math.pi
        mx, my = math.cos(theta + r), math.sin(theta + r)
        rho2 = rho - (nx * cx + ny * cy) + (mx * cx + my * cy)
        # theta is taken modulo pi, so flipping the normal flips rho with it.
        if (theta + r) % (2 * math.pi) != t2:
            rho2 = -rho2
        out.append((rho2, t2))
    return out


class TestFamilyAssignment:
    """Which image direction the yard lines take depends only on the camera.

    The detector used to bin lines into fixed bands -- yard lines assumed
    near-vertical, rows near-horizontal. On the Toledo drone footage the yard
    lines land at 84-90 degrees and the hash rows at ~4 and ~150, so every line
    was binned as a row, none as a yard line, and calibration returned
    no_calibration on every frame of every clip.
    """

    @staticmethod
    def _grid(h=360, w=640):
        verticals = [(float(x), 0.0) for x in (100, 250, 400, 550)]
        horizontals = [(80.0, math.pi / 2), (300.0, math.pi / 2)]
        return verticals + horizontals, (h, w)

    @pytest.mark.parametrize("degrees", [0, 15, 30, 45, 60, 75, 90, 120, 160])
    def test_finds_correspondences_at_any_orientation(self, degrees):
        # A grid is a grid however the camera is rolled. Rotating it must not
        # change whether the field is found.
        lines, shape = self._grid()
        result = yk.build_correspondences(_rot(lines, degrees), shape)
        assert result.has_enough(), (degrees, result.reason_codes)

    def test_the_larger_family_is_taken_as_the_yard_lines(self):
        # Not "the more vertical one": on real film the yard lines are the
        # near-horizontal family, and they are always the numerous one because
        # a yard line is a long solid stripe and a hash row is a few ticks.
        lines, shape = self._grid()
        result = yk.build_correspondences(lines, shape)
        # 4 yard lines x 2 rows.
        assert len(result.src_pts) == 8

    def test_families_need_not_be_perpendicular_in_the_image(self):
        # Perspective does not preserve angles. The two hash rows on the real
        # footage sit 146 degrees apart in the image, not 90.
        verticals = [(float(x), 0.0) for x in (100, 250, 400, 550)]
        # 40 degrees apart from each other, and neither perpendicular to the
        # yard lines -- the arrangement two converging hash rows actually make.
        skewed = [(179.5, math.radians(70.0)), (177.7, math.radians(110.0))]
        result = yk.build_correspondences(verticals + skewed, (360, 640))
        assert result.has_enough(), result.reason_codes


class TestDuplicateCollapse:
    def test_a_chain_of_near_duplicates_stays_one_line(self):
        # Hough returns many votes per painted stripe. Collapsing against the
        # last *kept* line lets a chain drift past the threshold and invent an
        # extra line -- which is then labelled as a yard line five yards along,
        # shifting every correspondence after it.
        chain = [(100.0 + i * 3.0, 0.0) for i in range(8)]
        others = [(400.0, 0.0), (550.0, 0.0)]
        rows = [(80.0, math.pi / 2), (300.0, math.pi / 2)]
        result = yk.build_correspondences(chain + others + rows, (360, 640))
        # 3 distinct yard lines (the chain collapses to one) x 2 rows.
        assert len(result.src_pts) == 6


class TestRowLabelling:
    """Sidelines are solid, hashes are dashed. That is what tells rows apart."""

    def test_two_dashed_rows_are_the_hashes(self):
        tmpl = default_template()
        ys = yk._match_rows_to_template([True, True], tmpl)
        assert ys == [pytest.approx(tmpl.hash_y_south), pytest.approx(tmpl.hash_y_north)]

    def test_two_solid_rows_are_the_sidelines(self):
        tmpl = default_template()
        ys = yk._match_rows_to_template([False, False], tmpl)
        assert ys == [
            pytest.approx(tmpl.sideline_y_south),
            pytest.approx(tmpl.sideline_y_north),
        ]

    def test_the_full_set_is_identified(self):
        tmpl = default_template()
        ys = yk._match_rows_to_template([False, True, True, False], tmpl)
        assert len(ys) == 4
        assert ys[0] == pytest.approx(tmpl.sideline_y_south)
        assert ys[-1] == pytest.approx(tmpl.sideline_y_north)

    def test_one_dashed_row_is_refused_rather_than_guessed(self):
        # It is either hash. Guessing puts the whole field 13 yards sideways,
        # and the fit reports a perfect inlier ratio either way.
        assert yk._match_rows_to_template([True], default_template()) is None

    def test_an_unlabelable_pattern_blocks_the_frame(self):
        rows = [(80.0, math.pi / 2), (200.0, math.pi / 2), (300.0, math.pi / 2)]
        verticals = [(float(x), 0.0) for x in (100, 250, 400)]
        result = yk.build_correspondences(verticals, (360, 640), dashed_lines=rows)
        assert "ambiguous_field_rows" in result.reason_codes
        assert not result.has_enough()

    def test_a_row_seen_both_ways_counts_as_solid(self):
        # The solid pass fits a whole stripe; the dashed pass a fragment of the
        # same one. Reporting it dashed would relabel a sideline as a hash.
        group = [(0.0, (10.0, 0.5, True)), (1.0, (10.1, 0.5, False))]
        assert yk._representative(group)[2] is False


class TestOffFrameStructures:
    def test_a_row_crossing_far_outside_the_frame_is_dropped(self):
        # Boundary paint and corner artifacts extend to meet the yard lines
        # 1700px off-frame. They yield no correspondence either way, but left
        # in they inflate the row pattern past anything a field can match.
        verticals = [(float(x), 0.0) for x in (100, 250, 400, 550)]
        rows = [(80.0, math.pi / 2), (300.0, math.pi / 2)]
        far = [(5000.0, math.radians(80.0))]
        result = yk.build_correspondences(verticals + rows + far, (360, 640))
        assert result.has_enough(), result.reason_codes
        assert len(result.src_pts) == 8


class TestProjectionPlausibilityGuard:
    def test_a_sane_homography_passes(self):
        H = np.array([[0.1, 0, 0], [0, 0.1, -20], [0, 0, 1]], dtype=np.float64)
        assert project.projects_onto_field(H, (720, 1280))

    def test_a_fit_placing_the_frame_off_the_planet_is_refused(self):
        # Observed on real footage: 6 of 10 inliers, frame centre 733 yards off
        # the side of the field. RANSAC is satisfied; the world is not.
        H = np.array([[0.1, 0, 0], [0, 10.0, -8000.0], [0, 0, 1]], dtype=np.float64)
        assert not project.projects_onto_field(H, (720, 1280))

    def test_a_degenerate_matrix_is_refused_rather_than_raising(self):
        H = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 0]], dtype=np.float64)
        assert not project.projects_onto_field(H, (720, 1280))
