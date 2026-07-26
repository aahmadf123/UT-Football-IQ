"""Yard-line keypoint detection (Issue #127 — light Hough+DLT path).

Pixel-only field-marking detection that feeds the DLT solver:

1. **White-paint mask** — HSV threshold (high V, low S) gated to the grass
   region, then morphological close to bridge dashed hashes.
2. **Hough lines** — ``cv2.HoughLines`` on Canny edges of the paint mask.
3. **Angle clustering** — group lines by orientation (yard lines vs
   sidelines/hashes) with a simple 5° angular tolerance (no sklearn).
4. **Correspondences** — match detected near-vertical lines (left→right) to
   evenly-spaced template yard lines, intersect with the dominant
   near-horizontal lines (sidelines/hashes), and label each crossing with its
   field-frame ``(x_yd, y_yd)`` coordinate.

OpenCV is imported lazily so the module stays importable (and partially
testable) in containers without cv2 — the clustering and correspondence math
are pure NumPy and unit-tested directly. The deep-keypoint upgrade
(PnLCalib / No-Bells-Just-Whistles) referenced in Issue #127 is a future
nightly-only variant and is intentionally **not** bundled here; this path is
the light Hough+DLT detector that is safe for the same-session window.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pipeline.homography.field_template import FieldTemplate, default_template

# Orientation tolerance for "these two lines belong to the same family"
# (radians). ~5° matches the DBSCAN ε in Issue #127.
ANGLE_TOL_RAD = math.radians(5.0)
# Widened when re-partitioning against the dominant family: perspective bends a
# family of parallel field lines apart as it recedes, and on real drone film the
# yard lines span ~6° across one frame.
FAMILY_TOL_RAD = math.radians(12.0)

# Dash bridging. Hash marks are individually tiny and set roughly a yard apart,
# so a row of them carries no long run of collinear edge pixels and the solid
# `cv2.HoughLines` pass never sees it. `HoughLinesP` with a gap this large jumps
# tick to tick and recovers the row.
DASH_BRIDGE_GAP_PX = 80
DASH_MIN_LEN_PX = 60
DASH_HOUGH_THRESHOLD = 50

# How far outside the frame a crossing may fall and still count, as a fraction
# of frame size. A field row that matters has to meet the yard lines somewhere
# near the visible play area; a line whose extension meets them 1700px off-frame
# is a corner artifact or a boundary marking, not a row of the field.
FRAME_MARGIN = 0.1

# Template rows, south → north, tagged with whether the marking is dashed.
# Sidelines are painted solid; inbound hashes are rows of separate ticks. That
# difference is what tells two detected rows apart, and getting it wrong puts
# every player ~13 yards off their true lateral position with a homography that
# fits its own (mislabelled) correspondences perfectly.
_ROW_IS_DASHED = (False, True, True, False)


@dataclass
class KeypointResult:
    """Detected correspondences + diagnostic features for confidence scoring."""

    src_pts: np.ndarray  # (N, 2) pixel coordinates
    dst_pts: np.ndarray  # (N, 2) field-yard coordinates
    line_count: int
    field_coverage: float
    yardline_angles: list[float] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)

    def has_enough(self) -> bool:
        return len(self.src_pts) >= 4


# ── White-paint + grass masks (cv2) ───────────────────────────────────────────


def grass_mask(frame: np.ndarray) -> tuple[np.ndarray, float]:
    """Return ``(mask, coverage)`` of the green playing surface via HSV."""
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower = np.array([35, 40, 40])
    upper = np.array([90, 255, 255])
    mask = cv2.inRange(hsv, lower, upper)
    coverage = float(np.count_nonzero(mask)) / float(mask.size)
    return mask, coverage


def white_paint_mask(frame: np.ndarray, grass: np.ndarray) -> np.ndarray:
    """White paint = high value, low saturation, dilated near the grass region."""
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower = np.array([0, 0, 170])
    upper = np.array([180, 60, 255])
    paint = cv2.inRange(hsv, lower, upper)
    # Only keep paint adjacent to grass (dilate grass to form a gate).
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    grass_gate = cv2.dilate(grass, kernel, iterations=1)
    paint = cv2.bitwise_and(paint, grass_gate)
    # Close to bridge dashed hash marks into continuous lines.
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    return cv2.morphologyEx(paint, cv2.MORPH_CLOSE, close_kernel)


def detect_hough_lines(paint: np.ndarray) -> list[tuple[float, float]]:
    """Return solid Hough lines as ``(rho, theta)`` from the paint mask edges."""
    import cv2

    edges = cv2.Canny(paint, 50, 150)
    raw = cv2.HoughLines(edges, 1, np.pi / 180, threshold=100)
    if raw is None:
        return []
    return [(float(r), float(t)) for r, t in raw[:, 0, :]]


def detect_dashed_lines(paint: np.ndarray) -> list[tuple[float, float]]:
    """Recover rows of *dashed* markings — the inbound hash rows.

    These are the second line family, and without them there is nothing for the
    yard lines to intersect. On broadcast-style film the sidelines supply that
    family; on drone film shot tight over the play the sidelines are out of
    frame entirely (the grass fills 94–97% of the frame) and the hash rows are
    all that is left.

    A row of ticks is invisible to the solid pass: each tick is a couple of
    hundred pixels of paint at most, and the run of collinear *edge* pixels
    along the row is near zero. `HoughLinesP` grows segments and will jump a gap
    of ``DASH_BRIDGE_GAP_PX``, which is what stitches the ticks into a row.

    Segments are returned in the same ``(rho, theta)`` form as the solid pass so
    both feed one clustering step.
    """
    import cv2

    edges = cv2.Canny(paint, 50, 150)
    segments = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=DASH_HOUGH_THRESHOLD,
        minLineLength=DASH_MIN_LEN_PX,
        maxLineGap=DASH_BRIDGE_GAP_PX,
    )
    if segments is None:
        return []
    out: list[tuple[float, float]] = []
    for x1, y1, x2, y2 in segments[:, 0, :]:
        theta = (math.atan2(float(y2 - y1), float(x2 - x1)) + math.pi / 2) % math.pi
        rho = float(x1) * math.cos(theta) + float(y1) * math.sin(theta)
        out.append((rho, theta))
    return out


# ── Pure-NumPy clustering + correspondence math (unit-tested directly) ─────────


def cluster_lines_by_angle(
    lines: list[tuple[float, float]], tol_rad: float = ANGLE_TOL_RAD
) -> list[list[tuple[float, float]]]:
    """Greedy 1-D clustering of ``(rho, theta)`` lines by orientation.

    Angles are taken modulo π (a line and its 180° flip are the same
    orientation). Returns clusters sorted by descending size.
    """
    if not lines:
        return []
    items = sorted(lines, key=lambda lt: lt[1] % math.pi)
    clusters: list[list[tuple[float, float]]] = []
    for rho, theta in items:
        t = theta % math.pi
        placed = False
        for cluster in clusters:
            ref = cluster[0][1] % math.pi
            diff = abs(t - ref)
            diff = min(diff, math.pi - diff)
            if diff <= tol_rad:
                cluster.append((rho, theta))
                placed = True
                break
        if not placed:
            clusters.append([(rho, theta)])
    clusters.sort(key=len, reverse=True)
    return clusters


def _intersect(
    l1: tuple[float, float], l2: tuple[float, float]
) -> tuple[float, float] | None:
    """Intersection point of two ``(rho, theta)`` lines, or ``None`` if parallel."""
    r1, t1 = l1
    r2, t2 = l2
    a = np.array([[math.cos(t1), math.sin(t1)], [math.cos(t2), math.sin(t2)]])
    b = np.array([r1, r2])
    det = float(np.linalg.det(a))
    if abs(det) < 1e-6:
        return None
    x, y = np.linalg.solve(a, b)
    return float(x), float(y)


def _angle_delta(a: float, b: float) -> float:
    """Smallest angle between two orientations, taken modulo π."""
    d = abs((a % math.pi) - (b % math.pi))
    return min(d, math.pi - d)


def _mean_angle(thetas: list[float]) -> float:
    """Circular mean of orientations modulo π.

    A plain arithmetic mean is wrong for a family straddling the wrap point:
    179° and 1° are 2° apart but average to 90°, which is perpendicular to both.
    Doubling maps the modulo-π circle onto a full circle, where the vector mean
    is well defined.
    """
    doubled = [2.0 * (t % math.pi) for t in thetas]
    x = sum(math.cos(d) for d in doubled)
    y = sum(math.sin(d) for d in doubled)
    return (math.atan2(y, x) / 2.0) % math.pi


def _param_along(reference: tuple[float, float], point: tuple[float, float]) -> float:
    """Signed position of ``point`` along ``reference``'s direction.

    Ordering a family by where its members cross one line of the *other* family
    is the orientation-agnostic replacement for "sort by x at mid-image". It is
    also projectively meaningful: perspective preserves order along a line, so
    rows sorted this way are in true across-field order whatever the camera did.
    """
    _, theta = reference
    dx, dy = -math.sin(theta), math.cos(theta)
    return point[0] * dx + point[1] * dy


def _closest_to_centre(
    lines: list[tuple[float, float, bool]], centre: tuple[float, float]
) -> tuple[float, float]:
    """The family member nearest the image centre, used as the sort axis.

    Near the centre the intersections with the other family are least likely to
    fall outside the frame, where lens distortion is worst and a small angular
    error moves the crossing point a long way.
    """
    cx, cy = centre
    best = min(
        lines,
        key=lambda ln: abs(ln[0] - (cx * math.cos(ln[1]) + cy * math.sin(ln[1]))),
    )
    return (best[0], best[1])


def _within_frame(point: tuple[float, float], w: int, h: int) -> bool:
    """Is a crossing inside the frame, allowing ``FRAME_MARGIN`` of slack?"""
    return (
        -FRAME_MARGIN * w <= point[0] <= (1 + FRAME_MARGIN) * w
        and -FRAME_MARGIN * h <= point[1] <= (1 + FRAME_MARGIN) * h
    )


def _order_and_dedupe(
    family: list[tuple[float, float, bool]],
    reference: tuple[float, float],
    min_separation: float,
    bounds: tuple[int, int],
) -> list[tuple[float, float, bool]]:
    """Sort a family across ``reference`` and collapse each painted line to one.

    Hough returns many votes per marking — one yard line arrives as a dozen
    ``(rho, theta)`` within a few pixels of each other — so without this the
    "yard lines" are a dozen copies of one stripe and the DLT is handed a
    degenerate correspondence set.

    Grouping is on the gap to the *immediately preceding* line, not to the last
    line kept. Comparing against the last kept lets a chain of near-duplicates
    drift: six votes spanning 18px are each within 15px of their neighbour but
    the sixth is 18px from the first, so it starts a second group. That phantom
    is worse than a duplicate, because the next step labels consecutive members
    as yard lines five yards apart -- one spurious line shifts every
    correspondence beyond it and quietly warps the whole fit.
    """
    w, h = bounds
    positioned: list[tuple[float, tuple[float, float, bool]]] = []
    for line in family:
        pt = _intersect(reference, (line[0], line[1]))
        if pt is None:  # parallel to the reference; no ordering available
            continue
        if not _within_frame(pt, w, h):
            # Off-frame crossings would contribute no correspondence anyway --
            # the intersection filter below discards them -- but leaving them in
            # inflates the row pattern, and a pattern of six rows matches no
            # arrangement of a field that has four.
            continue
        positioned.append((_param_along(reference, pt), line))
    if not positioned:
        return []
    positioned.sort(key=lambda p: p[0])

    groups: list[list[tuple[float, tuple[float, float, bool]]]] = [[positioned[0]]]
    for entry in positioned[1:]:
        if entry[0] - groups[-1][-1][0] < min_separation:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return [_representative(group) for group in groups]


def _representative(
    group: list[tuple[float, tuple[float, float, bool]]],
) -> tuple[float, float, bool]:
    """One line standing for a group of votes on the same painted marking.

    Prefers a solid observation: the solid pass fits a whole stripe, the dashed
    pass a bridged fragment. That choice also carries the solid/dashed tag used
    later to tell a sideline from a hash row, so a marking seen both ways must
    report as solid.
    """
    solid = [line for _, line in group if not line[2]]
    pool = solid or [line for _, line in group]
    return pool[len(pool) // 2]


def _match_rows_to_template(
    dashed_pattern: list[bool], tmpl: FieldTemplate
) -> list[float] | None:
    """Label detected cross-field rows by matching solid/dashed against the field.

    The four template rows south→north are sideline, hash, hash, sideline, and
    only the hashes are dashed. So the pattern of what was detected usually
    picks out exactly one subset: two dashed rows can only be the two hashes,
    and ``solid, dashed, dashed, solid`` can only be the full set.

    Returns ``None`` when more than one subset fits — one lone dashed row is
    either hash — rather than guessing. Guessing here is the expensive failure:
    the DLT fits mislabelled correspondences just as happily as correct ones and
    reports a high inlier ratio either way, so a wrong label survives every
    downstream check and simply moves the whole field.

    The result is in detected order, which fixes Y up to a reflection — see
    ``build_correspondences``.
    """
    rows_y = (
        tmpl.sideline_y_south,
        tmpl.hash_y_south,
        tmpl.hash_y_north,
        tmpl.sideline_y_north,
    )
    matches = [
        combo
        for combo in itertools.combinations(range(len(rows_y)), len(dashed_pattern))
        if [_ROW_IS_DASHED[i] for i in combo] == dashed_pattern
    ]
    if len(matches) != 1:
        return None
    return [float(rows_y[i]) for i in matches[0]]


def build_correspondences(
    lines: list[tuple[float, float]],
    frame_shape: tuple[int, int],
    template: FieldTemplate | None = None,
    *,
    dashed_lines: list[tuple[float, float]] | None = None,
) -> KeypointResult:
    """Match detected lines to the field template and emit pixel↔yard pairs.

    Strategy (regime-agnostic geometric core):
    - Take the largest angular cluster as the yard lines, whatever direction it
      points. Everything else is a candidate cross-field row.
    - Order each family by where it crosses a member of the other, and collapse
      duplicate Hough votes for the same painted line.
    - Map the yard lines onto consecutive template yard lines, and the rows onto
      whichever template rows their solid/dashed pattern identifies.
    - Every (yard line × row) intersection inside the frame becomes a labeled
      correspondence.

    This replaced a fixed pair of image-space bands — yard lines assumed
    near-vertical, rows near-horizontal — which encoded two assumptions that do
    not survive contact with real film. Neither family sits at a fixed image
    angle, because that depends only on where the camera is; and the two are not
    perpendicular *in the image*, because perspective does not preserve angles.
    On the Toledo drone footage the yard lines land at 84–90° and the two hash
    rows at ~4° and ~150°, so every detected line was binned as a row, no line
    was binned as a yard line, and calibration returned ``no_calibration`` on
    every frame of every clip.
    """
    tmpl = template or default_template()
    h, w = frame_shape[:2]
    centre = (w / 2.0, h / 2.0)
    reason_codes: list[str] = []

    tagged: list[tuple[float, float, bool]] = [(r, t, False) for r, t in lines]
    tagged += [(r, t, True) for r, t in (dashed_lines or [])]

    def _empty(code: str) -> KeypointResult:
        return KeypointResult(
            src_pts=np.empty((0, 2)),
            dst_pts=np.empty((0, 2)),
            line_count=len(lines),
            field_coverage=0.0,
            reason_codes=[*reason_codes, code],
        )

    if len(tagged) < 3:
        return _empty("insufficient_structured_lines")

    # The yard lines are the largest family. On real film it is not close --
    # 30-80 votes for the yard lines against 2-9 for everything else -- because
    # a yard line is a long solid stripe and a hash row is a handful of ticks.
    clusters = cluster_lines_by_angle([(r, t) for r, t, _ in tagged])
    dominant = _mean_angle([t for _, t in clusters[0]])
    yard: list[tuple[float, float, bool]] = []
    cross: list[tuple[float, float, bool]] = []
    for line in tagged:
        (yard if _angle_delta(line[1], dominant) <= FAMILY_TOL_RAD else cross).append(line)

    if len(yard) < 2 or not cross:
        return _empty("insufficient_structured_lines")

    # Sized between the scatter of Hough votes on one marking (~10px observed)
    # and the tightest real gap between two markings (~48px, where perspective
    # compresses the far yard lines). Erring large costs one correspondence;
    # erring small invents a line, which corrupts every label after it.
    diag = math.hypot(w, h)
    min_sep = max(8.0, 0.015 * diag)
    bounds = (w, h)

    # Rows first, and the yard lines are then ordered against a row that
    # survived filtering. Picking the reference from the raw cross family would
    # hand the yard lines a boundary artifact to sort against.
    cross_ordered = _order_and_dedupe(
        cross, _closest_to_centre(yard, centre), min_sep, bounds
    )
    if not cross_ordered:
        return _empty("insufficient_structured_lines")

    yard_ordered = _order_and_dedupe(
        yard, _closest_to_centre(cross_ordered, centre), min_sep, bounds
    )
    if len(yard_ordered) < 2:
        return _empty("insufficient_yard_lines")

    # Map detected yard lines onto consecutive template yard lines. Without
    # numeral OCR we anchor to a centered span of the template — enough for a
    # well-conditioned DLT; absolute yard offset is refined downstream.
    n_v = len(yard_ordered)
    yard_xs = list(tmpl.yard_lines_x)
    start = max(0, (len(yard_xs) - n_v) // 2)
    chosen_yard_x = yard_xs[start : start + n_v]
    if len(chosen_yard_x) < n_v:  # fewer template lines than detected
        chosen_yard_x = yard_xs[:n_v]
        yard_ordered = yard_ordered[: len(chosen_yard_x)]

    cross_ordered = cross_ordered[: len(_ROW_IS_DASHED)]
    chosen_rows = _match_rows_to_template([ln[2] for ln in cross_ordered], tmpl)
    if chosen_rows is None:
        return _empty("ambiguous_field_rows")

    # Both axes are recovered only up to a reflection: nothing in the markings
    # says which end zone is which, or which sideline is north. Enforcing a
    # right-handed homography downstream couples the two, leaving a single
    # 180° rotation. Lengths, separations and speeds are invariant under it;
    # only the *direction* of a gain is not, which is why the offset is
    # re-anchored from play context rather than from geometry.
    reason_codes.append("field_orientation_unanchored")

    src: list[list[float]] = []
    dst: list[list[float]] = []
    yardline_angles: list[float] = []
    for (v_rho, v_theta, _), x_yd in zip(yard_ordered, chosen_yard_x):
        yardline_angles.append(v_theta % math.pi)
        for (c_rho, c_theta, _), y_yd in zip(cross_ordered, chosen_rows):
            pt = _intersect((v_rho, v_theta), (c_rho, c_theta))
            if pt is None:
                continue
            px, py = pt
            if -0.1 * w <= px <= 1.1 * w and -0.1 * h <= py <= 1.1 * h:
                src.append([px, py])
                dst.append([float(x_yd), float(y_yd)])

    if len(src) < 4:
        reason_codes.append("insufficient_intersections")

    return KeypointResult(
        src_pts=np.asarray(src, dtype=np.float64) if src else np.empty((0, 2)),
        dst_pts=np.asarray(dst, dtype=np.float64) if dst else np.empty((0, 2)),
        line_count=len(lines),
        field_coverage=0.0,
        yardline_angles=yardline_angles,
        reason_codes=reason_codes,
    )


def detect_keypoints(
    frame: np.ndarray, template: FieldTemplate | None = None
) -> KeypointResult:
    """Full single-frame detection: masks → Hough → cluster → correspondences.

    Requires OpenCV. Failures degrade to an empty :class:`KeypointResult`
    with a reason code rather than raising, so the calibrate stage can record
    ``analytics_safe=False`` instead of crashing the pipeline.
    """
    try:
        import cv2  # noqa: F401
    except Exception:
        return KeypointResult(
            src_pts=np.empty((0, 2)),
            dst_pts=np.empty((0, 2)),
            line_count=0,
            field_coverage=0.0,
            reason_codes=["cv2_unavailable"],
        )

    grass, coverage = grass_mask(frame)
    reason_codes: list[str] = []
    if coverage < 0.25:
        reason_codes.append("low_field_coverage")
    paint = white_paint_mask(frame, grass)
    lines = detect_hough_lines(paint)
    if len(lines) < 4:
        return KeypointResult(
            src_pts=np.empty((0, 2)),
            dst_pts=np.empty((0, 2)),
            line_count=len(lines),
            field_coverage=coverage,
            reason_codes=reason_codes + ["insufficient_lines"],
        )

    # The dashed pass runs unconditionally rather than only as a fallback. Its
    # cost is one more Hough over an edge map already computed, and on footage
    # where the sidelines *are* visible it still adds the hash rows, which turns
    # a two-row fit into a four-row one across the full width of the field.
    dashed = detect_dashed_lines(paint)

    result = build_correspondences(lines, frame.shape[:2], template, dashed_lines=dashed)
    result.field_coverage = coverage
    result.reason_codes = reason_codes + result.reason_codes
    return result


def diagnostics(result: KeypointResult) -> dict[str, Any]:
    """Serializable summary of a detection for logging / debugging."""
    return {
        "n_correspondences": int(len(result.src_pts)),
        "line_count": int(result.line_count),
        "field_coverage": float(result.field_coverage),
        "reason_codes": list(result.reason_codes),
    }
