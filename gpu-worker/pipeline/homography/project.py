"""Project image points into field coordinates.

This is the step that was missing. The calibration stage fits a homography and
POSTs it to the backend, but nothing in the pipeline ever applied it to a player:
``field_x`` was read in a dozen modules and written in none. Every spatial metric
-- separation, cushion, speed, coverage, O-line gaps, route depth, workload --
therefore computed on absent coordinates, and did so *silently*, because the
readers all fall back with ``or 0``.

The field frame is the NCAA standard from :mod:`.field_template`: X ∈ [0, 100]
yards goal line to goal line, Y ∈ [-26.665, +26.665] yards sideline to sideline.
RANSAC fits pixel → yard directly, so projection needs no inversion.

The one real hazard here is shape. ``stage_calibrate`` serialises the matrix as a
flat 9-element list, ``stage_events`` is annotated for a nested 3×3, and the
artifact sink round-trips whatever it is given through JSON. Feeding a flat list
to ``np.asarray`` yields shape ``(9,)`` and ``H @ v`` raises. Everything that
takes a homography from outside this module should go through
:func:`homography_from_flat` first.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

__all__ = [
    "apply_homography",
    "apply_homography_many",
    "ground_anchor",
    "homography_from_flat",
    "projects_onto_field",
]

# How far outside the playing surface a camera may plausibly be looking, in
# yards. Generous on purpose: end zones, team areas and a wide sideline angle
# are all legitimate, so this is not a field-boundary test. It exists only to
# reject fits that are arithmetically fine and physically absurd.
PLAUSIBLE_X_YD = (-40.0, 140.0)
PLAUSIBLE_Y_YD = (-70.0, 70.0)


def homography_from_flat(value: Any) -> np.ndarray | None:
    """Coerce any accepted homography representation to a ``(3, 3)`` array.

    Accepts a flat 9-sequence, a nested 3×3, or an existing array; returns
    ``None`` for ``None`` or anything malformed. Returning ``None`` rather than
    raising is deliberate: a clip with an unusable calibration must still track,
    render, and produce clips -- it just cannot produce spatial metrics.

    Note the argument is typed ``Any`` on purpose. Callers hand this values that
    have been through JSON, and the point of the function is to be the one place
    that copes with that.
    """
    if value is None:
        return None
    try:
        arr = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return None

    if arr.shape == (9,):
        arr = arr.reshape(3, 3)
    if arr.shape != (3, 3):
        return None
    if not np.all(np.isfinite(arr)):
        return None
    return arr


def projects_onto_field(H: np.ndarray, frame_shape: tuple[int, ...]) -> bool:
    """Does this homography put the middle of the frame anywhere near a field?

    A degenerate fit can satisfy RANSAC and still be nonsense. On real footage
    one frame produced a homography with 6 of 10 correspondences as inliers that
    placed the frame centre 733 yards off the side of the field -- arithmetically
    consistent with its own inliers, physically impossible.

    That failure mode is the dangerous one, because inlier count is the only
    quality signal the fit reports and a wrong-but-consistent fit scores well on
    it. Everything downstream then treats the yard values as real.

    Only the centre is tested. Frame corners may legitimately project enormous
    distances -- near the horizon they approach infinity -- so bounding them
    would reject good calibrations of low camera angles, exactly the ones the
    "works at any camera height" goal depends on.
    """
    h, w = frame_shape[:2]
    x, y = apply_homography(H, (w / 2.0, h / 2.0))
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    return (
        PLAUSIBLE_X_YD[0] <= x <= PLAUSIBLE_X_YD[1]
        and PLAUSIBLE_Y_YD[0] <= y <= PLAUSIBLE_Y_YD[1]
    )


def apply_homography(H: np.ndarray, point: tuple[float, float]) -> tuple[float, float]:
    """Project one image point through a 3×3 homography to field coordinates."""
    v = np.array([point[0], point[1], 1.0], dtype=np.float64)
    p = H @ v
    if abs(p[2]) < 1e-12:
        # The point lies on the horizon line, where the projection is undefined.
        # Returning the unnormalised values keeps the caller from crashing; they
        # are meaningless, which is what the analytics_safe gate is for.
        return (float(p[0]), float(p[1]))
    return (float(p[0] / p[2]), float(p[1] / p[2]))


def apply_homography_many(
    H: np.ndarray, points: Sequence[tuple[float, float]] | Iterable[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Project many image points at once.

    One matrix multiply for the whole track instead of one per frame -- a clip
    carries 22 players × ~300 frames, so the per-point Python loop is the
    difference between milliseconds and seconds per clip.
    """
    pts = np.asarray(list(points), dtype=np.float64)
    if pts.size == 0:
        return []
    homogeneous = np.hstack([pts, np.ones((pts.shape[0], 1), dtype=np.float64)])
    projected = homogeneous @ H.T
    w = projected[:, 2]
    # Guard the horizon case elementwise rather than dropping the whole batch.
    safe = np.where(np.abs(w) < 1e-12, 1.0, w)
    xy = projected[:, :2] / safe[:, None]
    return [(float(x), float(y)) for x, y in xy]


def ground_anchor(bbox: Sequence[float]) -> tuple[float, float]:
    """The point on a detection that actually touches the field.

    A homography maps the *ground plane*, so the box centre is the wrong point
    to project: it floats at roughly waist height and lands the player a yard or
    two downfield of where they are, consistently and invisibly. The bottom-centre
    edge is the player's feet, which is the point the plane describes.
    """
    x1, _y1, x2, y2 = (float(v) for v in bbox[:4])
    return ((x1 + x2) / 2.0, y2)
