"""Pixel → field-yard projection.

This is the step whose absence made ``field_x`` a column that a dozen modules
read and none wrote, so every spatial metric computed on missing coordinates —
silently, because the readers all fall back with ``or 0``.
"""

from __future__ import annotations

import numpy as np
import pytest

from pipeline.homography.project import (
    apply_homography,
    apply_homography_many,
    ground_anchor,
    homography_from_flat,
)

IDENTITY_FLAT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
IDENTITY_NESTED = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


class TestHomographyFromFlat:
    """The normalizer exists because three shapes are in circulation.

    ``stage_calibrate`` serialises flat-9, ``stage_events`` is annotated
    nested-3x3, and the artifact sink round-trips whatever it is given through
    JSON. A flat list handed to ``np.asarray`` gives shape ``(9,)`` and ``H @ v``
    raises.
    """

    def test_accepts_flat_nine(self) -> None:
        result = homography_from_flat(IDENTITY_FLAT)
        assert result is not None
        assert result.shape == (3, 3)

    def test_accepts_nested_three_by_three(self) -> None:
        result = homography_from_flat(IDENTITY_NESTED)
        assert result is not None
        assert result.shape == (3, 3)

    def test_flat_and_nested_agree(self) -> None:
        flat = homography_from_flat(IDENTITY_FLAT)
        nested = homography_from_flat(IDENTITY_NESTED)
        assert flat is not None and nested is not None
        np.testing.assert_allclose(flat, nested)

    def test_accepts_an_existing_array(self) -> None:
        assert homography_from_flat(np.eye(3)) is not None

    def test_row_major_order(self) -> None:
        # Column-major would transpose the matrix and mirror the field.
        result = homography_from_flat([1, 2, 3, 4, 5, 6, 7, 8, 9])
        assert result is not None
        assert result[0][2] == 3.0
        assert result[2][0] == 7.0

    @pytest.mark.parametrize(
        "value",
        [None, [], [1, 2, 3], "not a matrix", [[1, 2], [3, 4]], {"a": 1}],
        ids=["none", "empty", "too-short", "string", "wrong-shape", "dict"],
    )
    def test_rejects_unusable_input(self, value: object) -> None:
        # None rather than raising: a clip with a bad calibration must still
        # track, render and produce clips — it just gets no spatial metrics.
        assert homography_from_flat(value) is None

    def test_rejects_non_finite(self) -> None:
        # A degenerate RANSAC fit can produce inf/nan; projecting through it
        # yields plausible-looking garbage rather than an obvious failure.
        assert homography_from_flat([1, 0, 0, 0, 1, 0, 0, 0, float("nan")]) is None
        assert homography_from_flat([1, 0, 0, 0, float("inf"), 0, 0, 0, 1]) is None


class TestApplyHomography:
    def test_identity_is_a_no_op(self) -> None:
        assert apply_homography(np.eye(3), (10.0, 20.0)) == (10.0, 20.0)

    def test_translation(self) -> None:
        H = np.array([[1, 0, 5], [0, 1, -3], [0, 0, 1]], dtype=np.float64)
        assert apply_homography(H, (10.0, 20.0)) == (15.0, 17.0)

    def test_perspective_divide(self) -> None:
        # The whole point of a homography over an affine transform.
        H = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 2]], dtype=np.float64)
        assert apply_homography(H, (10.0, 20.0)) == (5.0, 10.0)

    def test_survives_the_horizon(self) -> None:
        # w == 0 is the vanishing line, where projection is undefined. It must
        # not raise: one bad point should not kill a whole clip.
        H = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 0]], dtype=np.float64)
        result = apply_homography(H, (10.0, 20.0))
        assert len(result) == 2


class TestApplyHomographyMany:
    def test_matches_the_scalar_version(self) -> None:
        rng = np.random.default_rng(0)
        H = np.array([[1.2, 0.1, 30.0], [0.05, 0.9, -12.0], [1e-4, 2e-4, 1.0]])
        points = [(float(x), float(y)) for x, y in rng.uniform(0, 1280, size=(50, 2))]

        batched = apply_homography_many(H, points)
        one_by_one = [apply_homography(H, p) for p in points]

        np.testing.assert_allclose(np.array(batched), np.array(one_by_one), rtol=1e-12)

    def test_empty_input(self) -> None:
        assert apply_homography_many(np.eye(3), []) == []

    def test_horizon_points_do_not_poison_the_batch(self) -> None:
        # A whole track must not be lost because one frame projected onto the
        # vanishing line.
        H = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 0]], dtype=np.float64)
        result = apply_homography_many(H, [(1.0, 2.0), (3.0, 4.0)])
        assert len(result) == 2


class TestGroundAnchor:
    def test_uses_the_bottom_centre(self) -> None:
        # A homography maps the ground plane, so the projected point has to be
        # where the player touches it. Using the box centre puts every player a
        # yard or two downfield — consistently, and invisibly.
        assert ground_anchor([100.0, 50.0, 140.0, 130.0]) == (120.0, 130.0)

    def test_is_not_the_box_centre(self) -> None:
        bbox = [100.0, 50.0, 140.0, 130.0]
        centre_y = (bbox[1] + bbox[3]) / 2
        assert ground_anchor(bbox)[1] != centre_y

    def test_ignores_extra_bbox_fields(self) -> None:
        assert ground_anchor([0.0, 0.0, 10.0, 20.0, 0.9]) == (5.0, 20.0)
