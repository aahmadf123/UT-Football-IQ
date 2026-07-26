"""Where the playing surface ends -- and whether that edge is real.

The distinction the whole module turns on: turf covers 94-97% of the tight
Toledo clips, so the grass region "ends" at all four frame edges. That is where
the camera stopped, not where the field did. Reporting a confident outline for
such a frame would be worse than reporting none -- the caller would filter real
players out at the frame edge, and calibration would identify its cross-field
rows against a fiction.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.homography import field_boundary as fb

# A square well inside a 640x360 frame.
INSET = np.array([[100.0, 60.0], [500.0, 60.0], [500.0, 300.0], [100.0, 300.0]])
# The whole frame.
FULL = np.array([[0.0, 0.0], [640.0, 0.0], [640.0, 360.0], [0.0, 360.0]])


class TestPointPolygonDistance:
    def test_inside_is_positive(self) -> None:
        assert fb._point_polygon_distance(INSET, (300.0, 180.0)) > 0

    def test_outside_is_negative(self) -> None:
        assert fb._point_polygon_distance(INSET, (10.0, 180.0)) < 0

    def test_the_magnitude_is_the_distance_to_the_edge(self) -> None:
        assert fb._point_polygon_distance(INSET, (110.0, 180.0)) == pytest.approx(10.0)
        assert fb._point_polygon_distance(INSET, (80.0, 180.0)) == pytest.approx(-20.0)

    def test_a_corner_measures_to_the_vertex(self) -> None:
        # Not to the extended edge line -- projecting past a segment end would
        # under-report how far outside a corner a point is.
        d = fb._point_polygon_distance(INSET, (97.0, 56.0))
        assert d == pytest.approx(-5.0)

    def test_a_degenerate_polygon_contains_nothing(self) -> None:
        assert fb._point_polygon_distance(np.array([[0.0, 0.0], [1.0, 1.0]]), (0.5, 0.5)) < 0


class TestClippedEdges:
    def test_a_full_frame_polygon_is_clipped_on_every_side(self) -> None:
        assert set(fb._clipped_edges(FULL, 640, 360)) == set(fb.EDGE_NAMES)

    def test_an_inset_polygon_is_clipped_nowhere(self) -> None:
        assert fb._clipped_edges(INSET, 640, 360) == ()

    def test_one_real_edge_among_three_crops(self) -> None:
        # The wide Toledo frame: turf runs off the left, right and bottom, and
        # the far touchline is genuinely in view across the top.
        poly = np.array([[0.0, 100.0], [640.0, 120.0], [640.0, 360.0], [0.0, 360.0]])
        clipped = fb._clipped_edges(poly, 640, 360)
        assert "top" not in clipped
        assert {"left", "right", "bottom"} <= set(clipped)

    def test_touching_a_border_at_one_point_is_not_a_crop(self) -> None:
        # A real sideline can end at the frame edge. What marks a crop is a run
        # *along* the border, not a single vertex near it.
        poly = np.array([[0.0, 180.0], [500.0, 60.0], [500.0, 300.0]])
        assert "left" not in fb._clipped_edges(poly, 640, 360)


class TestContains:
    def _boundary(self, polygon: np.ndarray, clipped: tuple[str, ...]) -> fb.FieldBoundary:
        return fb.FieldBoundary(polygon=polygon, coverage=0.8, clipped_edges=clipped)

    def test_a_point_on_the_field(self) -> None:
        assert self._boundary(INSET, ()).contains((300.0, 180.0))

    def test_a_point_well_outside(self) -> None:
        assert not self._boundary(INSET, ()).contains((10.0, 180.0), margin=5.0)

    def test_just_outside_is_still_on_the_field(self) -> None:
        # Players are carried out of bounds and defenders chase into the bench
        # area; the boundary itself is only good to a few pixels.
        assert self._boundary(INSET, ()).contains((80.0, 180.0), margin=40.0)

    def test_everything_is_inside_when_no_edge_is_visible(self) -> None:
        # The tight-clip case. With the whole frame inside the surface there is
        # nothing to be outside of, and rejecting anything discards real players.
        fully_cropped = self._boundary(INSET, fb.EDGE_NAMES)
        assert fully_cropped.contains((10.0, 10.0), margin=0.0)
        assert not fully_cropped.has_visible_boundary


class TestDetectOnSyntheticFrames:
    @staticmethod
    def _frame(h: int = 360, w: int = 640, *, inset: int = 0) -> np.ndarray:
        pytest.importorskip("cv2")
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:] = (20, 20, 20)
        frame[inset : h - inset, inset : w - inset] = (40, 180, 40)  # BGR grass
        return frame

    def test_a_full_frame_of_turf_reports_no_visible_edge(self) -> None:
        pytest.importorskip("cv2")
        boundary = fb.detect_field_boundary(self._frame())
        assert boundary is not None
        assert not boundary.has_visible_boundary
        assert "no_visible_field_edge" in boundary.reason_codes

    def test_turf_inset_from_the_frame_reports_real_edges(self) -> None:
        pytest.importorskip("cv2")
        boundary = fb.detect_field_boundary(self._frame(inset=40))
        assert boundary is not None
        assert boundary.has_visible_boundary
        assert boundary.clipped_edges == ()

    def test_too_little_turf_is_refused(self) -> None:
        pytest.importorskip("cv2")
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        frame[:] = (20, 20, 20)
        frame[0:30, 0:30] = (40, 180, 40)  # a scrap of grass past the track
        assert fb.detect_field_boundary(frame) is None

    def test_a_frame_with_no_field_at_all(self) -> None:
        pytest.importorskip("cv2")
        assert fb.detect_field_boundary(np.zeros((360, 640, 3), dtype=np.uint8)) is None


class TestOnSurfaceVersusContains:
    """Two questions that must be allowed to disagree.

    ``contains`` answers "should this detection be discarded", so with no real
    edge visible it has to say yes to everything. ``on_surface`` answers "is
    this pixel turf", and calibration needs that one literal -- deciding whether
    a row has field on both sides is impossible if both sides always say yes.
    """

    def test_they_agree_when_a_real_edge_is_visible(self) -> None:
        b = fb.FieldBoundary(polygon=INSET, coverage=0.8, clipped_edges=())
        assert b.contains((300.0, 180.0)) and b.on_surface((300.0, 180.0))
        assert not b.contains((10.0, 180.0), margin=5.0)
        assert not b.on_surface((10.0, 180.0))

    def test_they_diverge_when_the_frame_is_all_field(self) -> None:
        b = fb.FieldBoundary(polygon=INSET, coverage=0.96, clipped_edges=fb.EDGE_NAMES)
        assert b.contains((10.0, 180.0)), "nothing to be outside of"
        assert not b.on_surface((10.0, 180.0)), "but the polygon is still a polygon"


class TestMarginScalesWithResolution:
    def test_a_bigger_frame_gets_a_proportionally_bigger_margin(self) -> None:
        # As a fixed pixel count the slack tightens as resolution rises, so the
        # identical shot would start rejecting players near the sideline purely
        # because it was recorded on a better camera.
        at_720p = fb.FieldBoundary(
            polygon=INSET, coverage=0.8, clipped_edges=(), frame_shape=(1280, 720)
        )
        at_4k = fb.FieldBoundary(
            polygon=INSET, coverage=0.8, clipped_edges=(), frame_shape=(3840, 2160)
        )
        assert at_720p.margin_px == pytest.approx(fb.CONTAINS_MARGIN_PX)
        assert at_4k.margin_px == pytest.approx(fb.CONTAINS_MARGIN_PX * 3)

    def test_an_unknown_frame_size_falls_back_to_the_quoted_value(self) -> None:
        b = fb.FieldBoundary(polygon=INSET, coverage=0.8, clipped_edges=())
        assert b.margin_px == pytest.approx(fb.CONTAINS_MARGIN_PX)


class TestDiagnostics:
    def test_none_is_serialisable(self) -> None:
        assert fb.diagnostics(None) == {"found": False}

    def test_it_reports_which_edges_are_real(self) -> None:
        boundary = fb.FieldBoundary(polygon=INSET, coverage=0.73, clipped_edges=("bottom",))
        d = fb.diagnostics(boundary)
        assert d["found"] is True
        assert d["visible_edges"] == ["left", "right", "top"]
        assert d["has_visible_boundary"] is True


# ── Consuming the boundary: dropping people who are not in the play ──────────


class TestOffFieldFilter:
    """Bench, staff and spectators are people the detector is right to find.

    Left in, each becomes a tracklet that inflates formation counts, joins
    coverage shells, and accrues workload against whoever re-ID eventually
    guesses. The wide Toledo frame has a row of staff standing off the turf on
    the left, well inside the image.
    """

    @staticmethod
    def _det(x: float, y: float) -> dict[str, object]:
        # A person-shaped box whose feet sit at (x, y).
        return {"bbox": [x - 10, y - 60, x + 10, y], "class": "player"}

    def _boundary(self, clipped: tuple[str, ...] = ()) -> fb.FieldBoundary:
        return fb.FieldBoundary(polygon=INSET, coverage=0.8, clipped_edges=clipped)

    def test_someone_off_the_surface_is_dropped(self) -> None:
        from pipeline.stage_detect import _off_field

        kept, dropped = _off_field([self._det(20.0, 180.0)], self._boundary())
        assert kept == [] and dropped == 1

    def test_a_player_on_the_field_is_kept(self) -> None:
        from pipeline.stage_detect import _off_field

        kept, dropped = _off_field([self._det(300.0, 200.0)], self._boundary())
        assert len(kept) == 1 and dropped == 0

    def test_it_tests_the_feet_not_the_box_centre(self) -> None:
        # A box centre sits a yard or so up the body, so someone standing just
        # off the sideline would be kept on the strength of their head.
        from pipeline.stage_detect import _off_field

        # The polygon's bottom edge is y=300. This box has its centre at y=290,
        # comfortably inside, and its feet at y=350 -- 50px out, past the 40px
        # tolerance. Someone standing just off the sideline, in other words.
        det = {"bbox": [300.0, 230.0, 320.0, 350.0], "class": "player"}
        boundary = self._boundary()
        assert boundary.contains((310.0, 290.0)), "the centre alone would be kept"
        kept, dropped = _off_field([det], boundary)
        assert dropped == 1, "but the feet are what decide"

    def test_nothing_is_dropped_when_no_touchline_is_visible(self) -> None:
        # The tight-clip case: the boundary is the frame border, everything is
        # nominally inside it, and rejecting anything discards real players.
        from pipeline.stage_detect import _off_field

        kept, dropped = _off_field(
            [self._det(20.0, 180.0)], self._boundary(clipped=fb.EDGE_NAMES)
        )
        assert len(kept) == 1 and dropped == 0

    def test_no_boundary_at_all_is_a_no_op(self) -> None:
        from pipeline.stage_detect import _off_field

        dets = [self._det(20.0, 180.0), self._det(300.0, 200.0)]
        kept, dropped = _off_field(dets, None)
        assert kept == dets and dropped == 0

    def test_a_detection_without_a_box_survives(self) -> None:
        from pipeline.stage_detect import _off_field

        kept, dropped = _off_field([{"class": "player"}], self._boundary())
        assert len(kept) == 1 and dropped == 0


class TestBoundaryRoundTrip:
    """The artifact goes through JSON, so what comes back is lists."""

    def test_it_survives_serialisation(self) -> None:
        original = fb.FieldBoundary(polygon=INSET, coverage=0.73, clipped_edges=("bottom",))
        restored = fb.boundary_from_diagnostics(fb.diagnostics(original))
        assert restored is not None
        assert restored.visible_edges == original.visible_edges
        assert restored.contains((300.0, 180.0))

    @pytest.mark.parametrize(
        "payload",
        [None, {}, {"found": False}, {"found": True}, {"found": True, "polygon": [[0, 0]]}],
        ids=["none", "empty", "not-found", "no-polygon", "degenerate"],
    )
    def test_unusable_payloads_yield_none(self, payload: object) -> None:
        # A clip with no boundary must still detect and track; it just filters
        # nothing.
        assert fb.boundary_from_diagnostics(payload) is None  # type: ignore[arg-type]


class TestTouchlines:
    """Only a real field edge may be offered to the calibrator as a sideline."""

    def test_a_fully_cropped_boundary_offers_none(self) -> None:
        # The tight-clip case. The polygon still has corners that do not
        # individually hug a border, and returning those would hand calibration
        # two confident "sidelines" that are nothing of the kind.
        b = fb.FieldBoundary(
            polygon=np.array([[0.0, 0.0], [640.0, 0.0], [640.0, 360.0], [0.0, 360.0]]),
            coverage=0.96,
            clipped_edges=fb.EDGE_NAMES,
            frame_shape=(640, 360),
        )
        assert b.touchlines() == []

    def test_a_real_edge_is_offered(self) -> None:
        b = fb.FieldBoundary(
            polygon=np.array([[0.0, 100.0], [640.0, 120.0], [640.0, 360.0], [0.0, 360.0]]),
            coverage=0.73,
            clipped_edges=("left", "right", "bottom"),
            frame_shape=(640, 360),
        )
        lines = b.touchlines()
        assert len(lines) == 1, "only the top edge turns away from the border"

    def test_no_frame_shape_means_no_claim(self) -> None:
        # Without the frame dimensions there is no way to tell a touchline from
        # a crop, so the honest answer is to offer nothing.
        b = fb.FieldBoundary(polygon=INSET, coverage=0.8, clipped_edges=())
        assert b.touchlines() == []

    def test_collinear_fragments_become_one_line(self) -> None:
        # A turf/track transition is ragged and the contour approximation breaks
        # one painted sideline into several short edges. Offered unmerged they
        # behave as several independent sidelines, and on the wide Toledo frame
        # that anchored four detected rows as sidelines -- which no field has.
        pieces = [
            (10.0, 100.0, 200.0, 101.0),
            (200.0, 101.0, 400.0, 99.0),
            (400.0, 99.0, 600.0, 100.0),
        ]
        merged = fb._merge_collinear(pieces, 735.0)
        assert len(merged) == 1
        rho, theta = merged[0]
        assert theta == pytest.approx(math.pi / 2, abs=0.02)
        assert rho == pytest.approx(100.0, abs=2.0)

    def test_edges_at_genuinely_different_angles_stay_apart(self) -> None:
        far_apart = [(0.0, 0.0, 300.0, 0.0), (0.0, 0.0, 0.0, 300.0)]
        assert len(fb._merge_collinear(far_apart, 735.0)) == 2

    def test_a_long_edge_outweighs_a_short_ragged_one(self) -> None:
        # The short pieces are exactly the ones whose angles are noise, so the
        # fit is weighted by length: averaging the two angles would land 2.3
        # degrees off horizontal, and the long edge pulls it back to 0.1.
        pieces = [(0.0, 100.0, 600.0, 100.0), (600.0, 100.0, 700.0, 108.0)]
        ((_, theta),) = fb._merge_collinear(pieces, 735.0)
        assert abs(math.degrees(theta) - 90.0) < 0.5

    def test_fragments_far_from_the_origin_still_merge(self) -> None:
        # Grouping on rho alone fails here: a degree of angular difference moves
        # rho by hundreds of pixels once an edge is far from the origin, so two
        # touching pieces of one sideline look like separate lines. A sideline
        # is exactly such an edge.
        pieces = [(1000.0, 900.0, 1400.0, 905.0), (1400.0, 905.0, 1800.0, 898.0)]
        assert len(fb._merge_collinear(pieces, 735.0)) == 1

    def test_the_line_form_matches_the_hough_convention(self) -> None:
        # A horizontal edge at y=100 is theta=pi/2, rho=100.
        b = fb.FieldBoundary(
            polygon=np.array([[10.0, 100.0], [600.0, 100.0], [600.0, 300.0], [10.0, 300.0]]),
            coverage=0.5,
            clipped_edges=(),
            frame_shape=(640, 360),
        )
        rho, theta = next(
            (r, t) for r, t in b.touchlines() if abs(t - math.pi / 2) < 0.01
        )
        assert rho == pytest.approx(100.0, abs=1.0)
