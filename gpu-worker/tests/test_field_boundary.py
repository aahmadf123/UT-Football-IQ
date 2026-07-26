"""Where the playing surface ends -- and whether that edge is real.

The distinction the whole module turns on: turf covers 94-97% of the tight
Toledo clips, so the grass region "ends" at all four frame edges. That is where
the camera stopped, not where the field did. Reporting a confident outline for
such a frame would be worse than reporting none -- the caller would filter real
players out at the frame edge, and calibration would identify its cross-field
rows against a fiction.
"""

from __future__ import annotations

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


class TestDiagnostics:
    def test_none_is_serialisable(self) -> None:
        assert fb.diagnostics(None) == {"found": False}

    def test_it_reports_which_edges_are_real(self) -> None:
        boundary = fb.FieldBoundary(polygon=INSET, coverage=0.73, clipped_edges=("bottom",))
        d = fb.diagnostics(boundary)
        assert d["found"] is True
        assert d["visible_edges"] == ["left", "right", "top"]
        assert d["has_visible_boundary"] is True
