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

    monkeypatch.setattr(
        yk, "detect_keypoints", lambda frame, template=None, **kwargs: _good_keypoints()
    )

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
    """Which rows were detected sets the lateral scale, so the evidence is ranked.

    What the boundary observed decides; the solid/dashed pattern only breaks a
    remaining tie. That ordering is not stylistic -- measured across rescalings
    of one real frame, the pattern reported ``SS``, ``DS`` and ``DD`` for the
    same two hash rows, while the observation said "interior" every time.
    """

    def test_two_dashed_rows_are_the_hashes(self):
        tmpl = default_template()
        ys, evidence = yk._match_rows_to_template([True, True], tmpl)
        assert ys == [pytest.approx(tmpl.hash_y_south), pytest.approx(tmpl.hash_y_north)]
        assert evidence == "row_identity_unverified", "the pattern alone decided it"

    def test_two_solid_rows_are_the_sidelines(self):
        tmpl = default_template()
        ys, _ = yk._match_rows_to_template([False, False], tmpl)
        assert ys == [
            pytest.approx(tmpl.sideline_y_south),
            pytest.approx(tmpl.sideline_y_north),
        ]

    def test_the_full_set_is_identified(self):
        tmpl = default_template()
        ys, evidence = yk._match_rows_to_template([False, True, True, False], tmpl)
        assert len(ys) == 4
        assert ys[0] == pytest.approx(tmpl.sideline_y_south)
        assert ys[-1] == pytest.approx(tmpl.sideline_y_north)
        assert evidence == "rows_observed", "four rows can only be all four"

    def test_two_interior_rows_are_the_hashes_whatever_the_pattern_says(self):
        # The case the rescaling experiment exposed. Both rows have playing
        # surface on either side, so neither can be a sideline -- and the only
        # two-row subset of interior rows is the pair of hashes. Read as the
        # sidelines instead, which is what the tag claimed at 0.5x, every
        # across-field measurement in the clip would double.
        tmpl = default_template()
        ys, evidence = yk._match_rows_to_template(
            [False, False], tmpl, [yk.ROW_INTERIOR, yk.ROW_INTERIOR]
        )
        assert ys == [pytest.approx(tmpl.hash_y_south), pytest.approx(tmpl.hash_y_north)]
        assert evidence == "rows_observed"

    def test_observation_is_not_overruled_by_the_pattern(self):
        # Three rows cannot all be interior -- the field has two inbound hash
        # rows. Refusing beats letting the pattern talk us into a third.
        assert (
            yk._match_rows_to_template(
                [True, True, True], default_template(), [yk.ROW_INTERIOR] * 3
            )
            is None
        )

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


class TestRowVerdictsFromTheSurface:
    """A sideline has no field beyond it. A hash row has field on both sides.

    That is the difference the footage actually supports, and it is what
    replaced asking which Hough pass found the line.
    """

    @staticmethod
    def _boundary(polygon, clipped=(), shape=(640, 360)):
        from pipeline.homography import field_boundary as fb

        return fb.FieldBoundary(
            polygon=np.asarray(polygon, dtype=float),
            coverage=0.8,
            clipped_edges=clipped,
            frame_shape=shape,
        )

    def test_a_row_with_turf_on_both_sides_is_interior(self):
        surface = self._boundary([[0, 0], [640, 0], [640, 360], [0, 360]])
        # A horizontal row across the middle of a frame that is entirely turf.
        assert yk._row_verdict((180.0, math.pi / 2), surface, (360, 640)) == yk.ROW_INTERIOR

    def test_a_row_on_the_edge_of_the_turf_is_a_sideline(self):
        surface = self._boundary([[0, 120], [640, 120], [640, 360], [0, 360]])
        assert yk._row_verdict((120.0, math.pi / 2), surface, (360, 640)) == yk.ROW_SIDELINE

    def test_no_boundary_means_no_verdict(self):
        assert yk._row_verdict((180.0, math.pi / 2), None, (360, 640)) == yk.ROW_UNKNOWN

    def test_an_axis_aligned_row_still_gets_a_span(self):
        # A perfectly horizontal line has cos(theta) == 0 and a vertical one has
        # sin(theta) == 0. Guarding the wrong one leaves the span empty and every
        # such row undecidable -- which is the orientation a bolted-down sideline
        # camera produces for the whole clip.
        assert yk._frame_span((180.0, math.pi / 2), 640, 360) is not None
        assert yk._frame_span((320.0, 0.0), 640, 360) is not None


class TestScalePlausibility:
    """A mislabelling is caught by comparing the two directions in one image.

    Nothing about the camera is assumed, which is the point: the footage has no
    fixed height, angle or resolution to check against.
    """

    def test_a_normal_grid_passes(self):
        src = [[0, 0], [100, 0], [0, 100], [100, 100]]
        dst = [[0, 0], [5, 0], [0, 5], [5, 5]]
        assert yk._scale_is_plausible(src, dst)

    def test_the_full_field_across_twenty_pixels_is_refused(self):
        # Two rows 22 px apart labelled as the two sidelines: 53.3 yd of field
        # in 22 px, while the yard lines in the same frame put 5 yd in 150.
        src = [[0, 0], [150, 0], [0, 22], [150, 22]]
        dst = [[0, -26.665], [5, -26.665], [0, 26.665], [5, 26.665]]
        assert not yk._scale_is_plausible(src, dst)

    def test_an_oblique_view_is_not_mistaken_for_a_mislabelling(self):
        # Perspective genuinely compresses one axis hard. The guard has to stay
        # out of the way of a real low-angle shot.
        src = [[0, 0], [300, 0], [0, 40], [300, 40]]
        dst = [[0, 0], [5, 0], [0, 13.33], [5, 13.33]]
        assert yk._scale_is_plausible(src, dst)


class TestResolutionInvariance:
    """The same scene at a different resolution must calibrate the same.

    Every threshold in this path is a length, and left as a plain pixel count
    each one encodes 720p. Rescaling one real frame by 3x used to turn its two
    hash rows solid, which relabels them as the two sidelines and doubles every
    across-field measurement in the clip -- silently, with more correspondences
    than the correct answer and no warning.
    """

    def test_thresholds_track_the_frame(self):
        at_720p = yk._px(80.0, yk.REFERENCE_DIAGONAL_PX)
        at_4k = yk._px(80.0, yk.REFERENCE_DIAGONAL_PX * 3)
        assert at_720p == pytest.approx(80.0)
        assert at_4k == pytest.approx(240.0)

    def test_a_threshold_never_collapses_to_nothing(self):
        assert yk._px(80.0, 1.0, minimum=10.0) == 10.0

    def test_the_same_geometry_labels_the_same_at_any_scale(self):
        # Correspondences scale with the image; the *field* coordinates they
        # carry must not move at all.
        verticals = [(float(x), 0.0) for x in (100, 250, 400, 550)]
        rows = [(80.0, math.pi / 2), (300.0, math.pi / 2)]
        base = yk.build_correspondences(verticals + rows, (360, 640))
        scaled = yk.build_correspondences(
            [(r * 3, t) for r, t in verticals + rows], (1080, 1920)
        )
        assert base.has_enough() and scaled.has_enough()
        np.testing.assert_allclose(
            np.sort(base.dst_pts, axis=0), np.sort(scaled.dst_pts, axis=0)
        )
        np.testing.assert_allclose(
            np.sort(base.src_pts, axis=0) * 3, np.sort(scaled.src_pts, axis=0)
        )


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


class TestSidelineAnchoring:
    """A visible touchline identifies a row outright instead of inferring it.

    Without it the labelling has only the solid/dashed pattern, which cannot
    separate "two hashes" (26.67 yd apart) from "a hash and a sideline" (13.335)
    -- and that choice sets the entire lateral scale. Guessing produces a
    homography that fits its own mislabelled correspondences perfectly and puts
    every player ~13 yards sideways.
    """

    def test_a_lone_dashed_row_is_still_refused_without_a_touchline(self) -> None:
        assert yk._match_rows_to_template([True], default_template()) is None

    def test_knowing_a_row_is_a_sideline_does_not_say_which_sideline(self) -> None:
        # The field is symmetric under a 180 degree rotation: both ends look
        # alike and both sidelines look alike. No boundary evidence closes that,
        # which is why `field_orientation_unanchored` is reported on every
        # frame rather than only on unanchored ones.
        tmpl = default_template()
        assert yk._match_rows_to_template([False], tmpl) is None
        assert yk._match_rows_to_template([False], tmpl, [yk.ROW_SIDELINE]) is None

    def test_a_pattern_contradicting_the_observation_yields_a_refusal(self) -> None:
        # The boundary puts row 0 on a real field edge; the paint reads it as
        # dashed, which no sideline is. The two signals disagree, and the
        # observation narrows to "a sideline and one of the hashes" without
        # saying which hash. Refusing is the cheap failure -- picking one would
        # move every player 13 yards sideways behind a perfect inlier ratio.
        assert (
            yk._match_rows_to_template(
                [True, True], default_template(), [yk.ROW_SIDELINE, yk.ROW_INTERIOR]
            )
            is None
        )

    def test_rows_are_matched_to_the_touchline_by_position_and_angle(self) -> None:
        rows = [(100.0, math.radians(90.0), False), (400.0, math.radians(90.0), True)]
        # A touchline sitting on the first row only.
        found = yk._sideline_indices(rows, [(102.0, math.radians(88.0))])
        assert found == {0}

    def test_a_touchline_far_from_every_row_anchors_nothing(self) -> None:
        rows = [(100.0, math.radians(90.0), False)]
        assert yk._sideline_indices(rows, [(900.0, math.radians(90.0))]) == set()

    def test_a_touchline_at_the_wrong_angle_anchors_nothing(self) -> None:
        # The far end line is a real boundary too, and it is not a sideline.
        rows = [(100.0, math.radians(90.0), False)]
        assert yk._sideline_indices(rows, [(100.0, math.radians(10.0))]) == set()

    def test_the_flipped_line_representation_still_matches(self) -> None:
        # (rho, theta) and (-rho, theta+pi) are the same line; a row and a
        # touchline derived by different routes can disagree on which they used.
        rows = [(-250.0, math.radians(2.0), False)]
        assert yk._sideline_indices(rows, [(250.0, math.radians(179.0))]) == {0}

    def test_the_reason_code_says_which_evidence_did_the_labelling(self) -> None:
        # A reader of the artifact has to be able to tell a labelling that was
        # measured from one that rested on the solid/dashed tag, because only
        # the second can silently double the lateral scale.
        verticals = [(float(x), 0.0) for x in (100, 250, 400, 550)]
        rows = [(80.0, math.pi / 2), (300.0, math.pi / 2)]
        assumed = yk.build_correspondences(verticals + rows, (360, 640))
        assert "row_identity_unverified" in assumed.reason_codes
        # The reflection is a real symmetry of the field, so it is reported
        # whether or not a touchline was seen.
        assert "field_orientation_unanchored" in assumed.reason_codes

    def test_one_touchline_may_not_anchor_two_rows(self) -> None:
        # Two rows a few pixels apart -- the two edges of one painted stripe --
        # both fall inside the touchline tolerance. Anchoring both would place
        # them 53.3 yd apart, which is how the wide Toledo frame produced a fit
        # asserting the full width of the field across 22 pixels.
        rows = [
            (100.0, math.radians(90.0), False),
            (108.0, math.radians(90.0), False),
        ]
        assert yk._sideline_indices(rows, [(104.0, math.radians(90.0))]) == set()

    def test_a_boundary_corner_is_not_offered_as_a_sideline(self) -> None:
        # Where the field ends the outline turns and runs along the end line,
        # and the turf mask throws off spikes at worn patches. Those are real
        # boundary and none of them is a sideline; on the wide frame four of the
        # five candidate edges were of that kind.
        rows = [(100.0, math.radians(90.0), False), (400.0, math.radians(92.0), True)]
        touchlines = [
            (100.0, math.radians(90.0)),  # along the rows -- a plausible sideline
            (250.0, math.radians(30.0)),  # a corner
            (-400.0, math.radians(150.0)),  # a mask spike
        ]
        assert yk._plausible_touchlines(touchlines, rows) == [(100.0, math.radians(90.0))]
