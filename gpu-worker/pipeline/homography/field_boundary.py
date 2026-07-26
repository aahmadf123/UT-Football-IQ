"""Where the playing surface ends, in image space.

Two things need this and neither had it. Detections outside the field are not
players in the play -- they are bench, staff, spectators, and the occasional
piece of equipment -- and every one of them currently becomes a tracklet that
carries into formation counts, coverage shells and workload. And calibration
has to *assume* which cross-field rows it detected, because with no sideline to
measure against, two dashed rows could be the two hashes or a hash and a
sideline, and that choice sets the whole lateral scale.

The central distinction here is between a boundary and the edge of the frame.
Turf covers 94-97% of the tight Toledo clips, so the grass region "ends" at all
four frame edges -- but that is where the camera stopped, not where the field
did. An edge that runs along the frame border carries no information; an edge
that turns away from it is a real touchline. Conflating the two would report a
confident field outline for footage that contains no boundary at all, which is
worse than reporting none: the caller would then filter real players out at the
frame edge, and calibration would identify rows against a fiction.

OpenCV is imported lazily so the module stays importable in containers without
cv2, matching :mod:`.yardline_keypoints`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: Fraction of a frame dimension within which a boundary vertex counts as
#: sitting *on* that frame edge rather than turning away from it.
EDGE_TOUCH_FRACTION = 0.02

#: Below this share of the frame the "field" is too small to be the playing
#: surface -- a patch of grass past the track, or a badly thresholded frame.
MIN_FIELD_COVERAGE = 0.15

#: Polygon simplification, as a fraction of the contour perimeter. Large enough
#: to drop the ragged pixel-level detail of a turf/track transition, small
#: enough to keep a real corner.
POLY_EPSILON_FRACTION = 0.01

#: How far outside the polygon a detection may sit and still be on the field.
#: Players are carried out of bounds and defenders chase into the bench area,
#: and the boundary itself is only accurate to a few pixels.
CONTAINS_MARGIN_PX = 40.0

EDGE_NAMES = ("left", "right", "top", "bottom")


@dataclass
class FieldBoundary:
    """The playing surface outline, and how much of it is genuinely visible."""

    #: (N, 2) polygon vertices in image pixels, in contour order.
    polygon: np.ndarray
    #: Share of the frame the surface occupies.
    coverage: float
    #: Frame edges the surface runs along, i.e. where it is merely cropped.
    clipped_edges: tuple[str, ...] = ()
    reason_codes: list[str] = field(default_factory=list)

    @property
    def visible_edges(self) -> tuple[str, ...]:
        """Frame sides where a real touchline was found rather than a crop."""
        return tuple(name for name in EDGE_NAMES if name not in self.clipped_edges)

    @property
    def has_visible_boundary(self) -> bool:
        """Is any real field edge in frame?

        False for the tight drone clips, and that is the useful answer: it tells
        calibration it has nothing to identify its cross-field rows against, and
        tells the detection filter not to reject anything.
        """
        return bool(self.visible_edges)

    def contains(self, point: tuple[float, float], margin: float = CONTAINS_MARGIN_PX) -> bool:
        """Is a point on the field, allowing ``margin`` pixels of slack?

        Always ``True`` when no real edge is visible. With the whole frame
        inside the surface there is nothing to be outside *of*, and answering
        ``False`` for anything would discard real players.
        """
        if not self.has_visible_boundary:
            return True
        return _point_polygon_distance(self.polygon, point) >= -margin


def _point_polygon_distance(polygon: np.ndarray, point: tuple[float, float]) -> float:
    """Signed distance from ``point`` to ``polygon``: positive inside.

    Pure NumPy so the containment test -- the part called once per detection --
    stays usable without cv2 and stays testable directly.
    """
    px, py = float(point[0]), float(point[1])
    pts = np.asarray(polygon, dtype=np.float64)
    if pts.shape[0] < 3:
        return -math.inf

    nxt = np.roll(pts, -1, axis=0)
    seg = nxt - pts
    rel = np.array([px, py], dtype=np.float64) - pts
    seg_len_sq = np.einsum("ij,ij->i", seg, seg)
    # Clamp the projection onto each edge to the segment, so the nearest point
    # of a corner is the vertex rather than a point off the extended line.
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(seg_len_sq > 0, np.einsum("ij,ij->i", rel, seg) / seg_len_sq, 0.0)
    t = np.clip(t, 0.0, 1.0)
    nearest = pts + seg * t[:, None]
    distance = float(np.min(np.linalg.norm(nearest - np.array([px, py]), axis=1)))

    # Ray casting for the sign. Counting crossings of a horizontal ray is
    # independent of the distance computation above, so a point exactly on an
    # edge gets distance ~0 whichever side the parity lands on.
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, nxt, strict=True):
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (py - y1) / (y2 - y1) * (x2 - x1)
            if px < x_cross:
                inside = not inside
    return distance if inside else -distance


def _clipped_edges(polygon: np.ndarray, width: int, height: int) -> tuple[str, ...]:
    """Which frame edges the polygon runs along rather than turning away from.

    A vertex near the border is not enough -- a real sideline can end at the
    frame edge. What marks a crop is a *run* along the border, so this asks
    whether the polygon has points spanning most of that edge.
    """
    pts = np.asarray(polygon, dtype=np.float64)
    tol_x = max(2.0, width * EDGE_TOUCH_FRACTION)
    tol_y = max(2.0, height * EDGE_TOUCH_FRACTION)
    out: list[str] = []

    for name, mask, along, extent in (
        ("left", pts[:, 0] <= tol_x, pts[:, 1], height),
        ("right", pts[:, 0] >= width - tol_x, pts[:, 1], height),
        ("top", pts[:, 1] <= tol_y, pts[:, 0], width),
        ("bottom", pts[:, 1] >= height - tol_y, pts[:, 0], width),
    ):
        on_edge = along[mask]
        if on_edge.size >= 2 and (on_edge.max() - on_edge.min()) >= 0.5 * extent:
            out.append(name)
    return tuple(out)


def detect_field_boundary(frame: np.ndarray) -> FieldBoundary | None:
    """Outline the playing surface, or ``None`` if it cannot be found.

    ``None`` and "found but entirely cropped" are different answers and the
    caller needs both: the first means the frame could not be read as a field
    at all, the second that it is all field.
    """
    try:
        import cv2
    except Exception:
        return None

    from pipeline.homography.yardline_keypoints import grass_mask

    mask, coverage = grass_mask(frame)
    if coverage < MIN_FIELD_COVERAGE:
        return None

    # Close over players, paint and shadows so the surface is one region rather
    # than a colander. The kernel is sized to the frame so it behaves the same
    # at 720p and 4K.
    h, w = frame.shape[:2]
    k = max(5, int(min(h, w) * 0.02) | 1)
    closed = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    )

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < MIN_FIELD_COVERAGE * h * w:
        return None

    epsilon = POLY_EPSILON_FRACTION * cv2.arcLength(largest, True)
    polygon = cv2.approxPolyDP(largest, epsilon, True).reshape(-1, 2).astype(np.float64)
    if polygon.shape[0] < 3:
        return None

    clipped = _clipped_edges(polygon, w, h)
    reason_codes: list[str] = []
    if len(clipped) == len(EDGE_NAMES):
        # Every side is a crop: the camera is looking at field and nothing but.
        reason_codes.append("no_visible_field_edge")

    return FieldBoundary(
        polygon=polygon,
        coverage=float(coverage),
        clipped_edges=clipped,
        reason_codes=reason_codes,
    )


def diagnostics(boundary: FieldBoundary | None) -> dict[str, Any]:
    """Serializable summary for the calibration artifact and the review UI."""
    if boundary is None:
        return {"found": False}
    return {
        "found": True,
        "coverage": round(boundary.coverage, 4),
        "vertices": int(boundary.polygon.shape[0]),
        "clipped_edges": list(boundary.clipped_edges),
        "visible_edges": list(boundary.visible_edges),
        "has_visible_boundary": boundary.has_visible_boundary,
        "reason_codes": list(boundary.reason_codes),
    }
