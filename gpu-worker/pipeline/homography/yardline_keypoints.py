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

# ── Length thresholds, and why none of them is a plain pixel count ────────────
#
# Every threshold below was tuned on 720p drone film, and every one of them is a
# *length*: how many edge pixels make a line, how long a dash is, how far apart
# two markings sit. Left as absolute pixels they encode the capture resolution,
# and the footage will not have one -- angle, height, resolution and recording
# method all vary shot to shot.
#
# The failure is silent and expensive. Feed the same frame in at 3x and a hash
# tick becomes three times longer, clears the *solid* Hough threshold, and the
# row reports as solid paint. Solid rows are sidelines, so two hash rows 26.7 yd
# apart get labelled as two sidelines 53.3 yd apart: every lateral measurement
# in the clip comes out 2x wrong, from a fit that has more correspondences than
# the correct one and raises no warning.
#
# So thresholds are stated at their tuned 720p value and scaled to the frame in
# hand. Scaling *all* of them together is what matters -- it makes detection
# commute with image scaling, so a tick too short to be solid at 720p is still
# too short at 4K.
REFERENCE_DIAGONAL_PX = math.hypot(1280, 720)


def _px(reference_px: float, diagonal: float, minimum: float = 1.0) -> float:
    """A threshold tuned at 720p, expressed for a frame of this diagonal."""
    return max(minimum, reference_px * diagonal / REFERENCE_DIAGONAL_PX)


def _diagonal(shape: tuple[int, ...]) -> float:
    return math.hypot(float(shape[1]), float(shape[0]))


# Solid-line Hough. The threshold counts edge pixels lying on the line, so it is
# a length; the rho resolution is one too, and holding it at 1px would spread a
# real line's votes across more bins at higher resolution and lower its peak.
SOLID_HOUGH_THRESHOLD = 100
SOLID_HOUGH_RHO_PX = 1.0

# Dash bridging. Hash marks are individually tiny and set roughly a yard apart,
# so a row of them carries no long run of collinear edge pixels and the solid
# `cv2.HoughLines` pass never sees it. `HoughLinesP` with a gap this large jumps
# tick to tick and recovers the row.
DASH_BRIDGE_GAP_PX = 80
DASH_MIN_LEN_PX = 60
DASH_HOUGH_THRESHOLD = 50

# Paint-mask morphology: the grass gate dilation, and the close that joins the
# two edges of one painted stripe into a single blob.
PAINT_GRASS_GATE_PX = 15
PAINT_CLOSE_PX = 5

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
    diag = _diagonal(frame.shape)
    # Only keep paint adjacent to grass (dilate grass to form a gate).
    gate_px = _odd(_px(PAINT_GRASS_GATE_PX, diag, minimum=3.0))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gate_px, gate_px))
    grass_gate = cv2.dilate(grass, kernel, iterations=1)
    paint = cv2.bitwise_and(paint, grass_gate)
    # Close to bridge dashed hash marks into continuous lines.
    close_px = _odd(_px(PAINT_CLOSE_PX, diag, minimum=3.0))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (close_px, close_px))
    return cv2.morphologyEx(paint, cv2.MORPH_CLOSE, close_kernel)


def _odd(value: float) -> int:
    """Nearest odd integer >= 3 -- structuring elements need a centre pixel."""
    return max(3, int(round(value)) | 1)


def _rows(result: np.ndarray, width: int) -> np.ndarray:
    """Normalise a Hough result to ``(N, width)``.

    OpenCV 4 returns ``(N, 1, width)`` from the Hough functions; OpenCV 5
    dropped the middle axis and returns ``(N, width)``. The repo pins 4.10, so
    indexing ``[:, 0, :]`` is correct today and raises ``IndexError`` the moment
    anything pulls in 5 -- which installing ultralytics does, since it depends
    on opencv unpinned. Reshaping covers both without caring which is present.
    """
    return result.reshape(-1, width)


def detect_hough_lines(paint: np.ndarray) -> list[tuple[float, float]]:
    """Return solid Hough lines as ``(rho, theta)`` from the paint mask edges."""
    import cv2

    diag = _diagonal(paint.shape)
    edges = cv2.Canny(paint, 50, 150)
    raw = cv2.HoughLines(
        edges,
        _px(SOLID_HOUGH_RHO_PX, diag),
        np.pi / 180,
        threshold=int(_px(SOLID_HOUGH_THRESHOLD, diag, minimum=20.0)),
    )
    if raw is None:
        return []
    return [(float(r), float(t)) for r, t in _rows(raw, 2)]


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

    diag = _diagonal(paint.shape)
    edges = cv2.Canny(paint, 50, 150)
    segments = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=int(_px(DASH_HOUGH_THRESHOLD, diag, minimum=10.0)),
        minLineLength=_px(DASH_MIN_LEN_PX, diag),
        maxLineGap=_px(DASH_BRIDGE_GAP_PX, diag),
    )
    if segments is None:
        return []
    out: list[tuple[float, float]] = []
    for x1, y1, x2, y2 in _rows(segments, 4):
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


#: How far to either side of a row to look for playing surface, at 720p. Far
#: enough to clear the painted stripe itself and the ragged edge of the turf
#: mask, short enough that the answer is still about *this* row.
SIDE_PROBE_PX = 45.0
#: Points sampled along a row's visible span before taking the majority verdict.
#: A single sample would be decided by whoever happened to be standing there.
SIDE_PROBE_SAMPLES = 9

ROW_SIDELINE = "sideline"
ROW_INTERIOR = "interior"
ROW_UNKNOWN = "unknown"


def _frame_span(
    line: tuple[float, float], w: int, h: int
) -> tuple[np.ndarray, np.ndarray] | None:
    """Where a ``(rho, theta)`` line enters and leaves the frame, or ``None``."""
    rho, theta = line
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    hits: list[np.ndarray] = []
    # Solving for x on the horizontal borders needs cos != 0, and for y on the
    # vertical borders needs sin != 0 -- guarding each on the *other* one leaves
    # an exactly horizontal or vertical line with no span at all, which is the
    # orientation a fixed-sideline camera produces.
    if abs(cos_t) > 1e-9:
        for y in (0.0, float(h)):
            x = (rho - y * sin_t) / cos_t
            if -1.0 <= x <= w + 1.0:
                hits.append(np.array([x, y]))
    if abs(sin_t) > 1e-9:
        for x in (0.0, float(w)):
            y = (rho - x * cos_t) / sin_t
            if -1.0 <= y <= h + 1.0:
                hits.append(np.array([x, y]))
    if len(hits) < 2:
        return None
    pts = np.asarray(hits)
    # The two furthest apart are the entry and exit; the others are duplicates
    # where the line passes through a corner.
    best = max(
        itertools.combinations(range(len(pts)), 2),
        key=lambda ij: float(np.linalg.norm(pts[ij[0]] - pts[ij[1]])),
    )
    p0, p1 = pts[best[0]], pts[best[1]]
    return (p0, p1) if float(np.linalg.norm(p1 - p0)) > 1.0 else None


def _row_verdict(
    line: tuple[float, float],
    boundary: Any,
    frame_shape: tuple[int, int],
) -> str:
    """Is this row a sideline, an interior row, or undecidable — by observation.

    A sideline is the edge of the playing surface: there is no field beyond it.
    An inbound hash row has field on both sides. That is a *visible* difference,
    and unlike the solid-vs-dashed distinction it is one this footage actually
    supports.

    The alternative, which this replaces, was to infer identity from whether the
    solid Hough pass happened to find the line. That tag is not a property of
    the paint but of the threshold: rescaling one frame flips hash rows to
    "solid" and back, and because the tag decides whether two rows are the
    hashes (26.7 yd apart) or the sidelines (53.3 yd), the flip silently doubles
    every lateral measurement in the clip -- from a fit that reports more
    correspondences and no warning. Measured on the Toledo film, no continuity
    statistic separates the two populations: known-solid yard lines span
    0.03-1.00 of longest-run fraction against 0.01-0.90 for the mixed rows.

    Known limitation: on a field with grass beyond the sidelines the surface
    does not end at the paint, so a real sideline reads as ``interior``. The
    caller sees ``unknown`` and ``interior`` differently for exactly this reason
    -- ``interior`` is only asserted where the turf mask is trustworthy.
    """
    w, h = frame_shape[1], frame_shape[0]
    span = _frame_span(line, w, h)
    if span is None or boundary is None:
        return ROW_UNKNOWN
    p0, p1 = span
    probe = _px(SIDE_PROBE_PX, math.hypot(w, h))
    normal = np.array([math.cos(line[1]), math.sin(line[1])]) * probe

    votes: list[str] = []
    for t in np.linspace(0.15, 0.85, SIDE_PROBE_SAMPLES):
        centre = p0 + (p1 - p0) * t
        a, b = centre + normal, centre - normal
        if not (_inside(a, w, h) and _inside(b, w, h)):
            # One side is off-frame, so "is there field beyond it" is not a
            # question this frame can answer.
            continue
        on_a = boundary.on_surface((float(a[0]), float(a[1])))
        on_b = boundary.on_surface((float(b[0]), float(b[1])))
        if on_a and on_b:
            votes.append(ROW_INTERIOR)
        elif on_a != on_b:
            votes.append(ROW_SIDELINE)
    if not votes:
        return ROW_UNKNOWN
    verdict = max(set(votes), key=votes.count)

    if verdict == ROW_SIDELINE and not boundary.has_visible_boundary:
        # The boundary has already established that no field edge is in view --
        # the surface runs off all four sides of the frame. A probe landing
        # outside the polygon here is a hole in the turf mask, a shadow or a
        # worn patch, not the end of the field, and calling it a sideline is the
        # expensive direction to be wrong in.
        return ROW_UNKNOWN
    return verdict


def _inside(point: np.ndarray, w: int, h: int) -> bool:
    return 0.0 <= float(point[0]) <= w and 0.0 <= float(point[1]) <= h


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


#: How close a detected row must sit to a boundary touchline, in angle and in
#: perpendicular offset, to be considered the same painted line. The offset is
#: quoted at 720p and scaled with the frame, like every other length here.
TOUCHLINE_ANGLE_TOL_RAD = math.radians(8.0)
TOUCHLINE_OFFSET_TOL_PX = 30.0


def _row_verdicts(
    rows: list[tuple[float, float, bool]],
    boundary: Any,
    frame_shape: tuple[int, int],
    touchlines: list[tuple[float, float]] | None = None,
) -> list[str]:
    """Classify each detected row against what the boundary can actually see.

    Two independent observations, strongest first: lying on a real touchline
    makes a row a sideline outright, and failing that, having playing surface on
    both sides rules a sideline out.
    """
    diagonal = math.hypot(float(frame_shape[1]), float(frame_shape[0]))
    usable = _plausible_touchlines(touchlines or [], rows)
    on_touchline = _sideline_indices(rows, usable, diagonal)
    if len(on_touchline) > 2:
        # A field has two sidelines. More rows than that landing on a
        # "touchline" means the boundary was mis-read, so the evidence is
        # dropped rather than allowed to pin a labelling it cannot support.
        on_touchline = set()
    return [
        ROW_SIDELINE
        if i in on_touchline
        else _row_verdict((rho, theta), boundary, frame_shape)
        for i, (rho, theta, _) in enumerate(rows)
    ]


def _plausible_touchlines(
    touchlines: list[tuple[float, float]], rows: list[tuple[float, float, bool]]
) -> list[tuple[float, float]]:
    """Keep only boundary edges that could actually be a sideline.

    The surface outline is not all sideline. Where the field ends, the polygon
    turns a corner and runs along the end line; the turf mask also throws off
    spikes at shadows and worn patches. On the wide Toledo frame the far
    sideline is one edge at 91°, and the four others -- corners at 62° and 119°,
    and a mask spike -- are boundary in the sense that turf stops there, but no
    sideline lies at those angles.

    Sidelines belong to the cross-field family by definition, so an edge that
    does not share that family's orientation is something else. Left in, each
    is a chance for a row to anchor to a corner and be declared a sideline.
    """
    if not rows or not touchlines:
        return []
    family = _mean_angle([theta for _, theta, _ in rows])
    return [ln for ln in touchlines if _angle_delta(ln[1], family) <= FAMILY_TOL_RAD]


def _sideline_indices(
    rows: list[tuple[float, float, bool]],
    touchlines: list[tuple[float, float]],
    diagonal: float = REFERENCE_DIAGONAL_PX,
) -> set[int]:
    """Which ordered rows coincide with a real field boundary edge.

    This is the whole point of detecting the boundary. Without it the row
    labelling infers identity from the solid/dashed pattern alone, which cannot
    separate "two hashes" from "a hash and a sideline" -- and that choice sets
    the entire lateral scale. A row lying on a touchline is not inferred to be a
    sideline; it is observed to be one.
    """
    tol = _px(TOUCHLINE_OFFSET_TOL_PX, diagonal)
    matches: dict[int, set[int]] = {}
    for i, (rho, theta, _) in enumerate(rows):
        for j, (t_rho, t_theta) in enumerate(touchlines):
            if _angle_delta(theta, t_theta) > TOUCHLINE_ANGLE_TOL_RAD:
                continue
            # rho is signed against the normal direction, so a line and its
            # flipped representation differ in sign as well as angle.
            same = abs(rho - t_rho)
            flipped = abs(rho + t_rho)
            if min(same, flipped) <= tol:
                matches.setdefault(j, set()).add(i)

    # One painted sideline is one row. A touchline that fits two rows has not
    # identified either of them -- it means the tolerance swallowed the gap
    # between them -- so that evidence is dropped rather than used to declare
    # both rows sidelines, which would place them 53.3 yd apart when they are
    # adjacent.
    return {next(iter(rs)) for rs in matches.values() if len(rs) == 1}


def _match_rows_to_template(
    dashed_pattern: list[bool],
    tmpl: FieldTemplate,
    verdicts: list[str] | None = None,
) -> tuple[list[float], str] | None:
    """Label detected cross-field rows, and say what evidence did the labelling.

    The four template rows south→north are sideline, hash, hash, sideline. Which
    subset was detected sets the entire lateral scale: two rows read as the
    hashes are 26.7 yd apart, the same two read as the sidelines are 53.3 yd, so
    a mislabelling doubles every across-field measurement in the clip. The DLT
    fits mislabelled correspondences as happily as correct ones and reports a
    high inlier ratio either way, so nothing downstream catches it.

    Evidence is therefore ranked, not pooled:

    1. **What the boundary observed.** A row on a real touchline is a sideline;
       a row with playing surface on both sides is not. This is measured.
    2. **The solid/dashed pattern**, and only to break a remaining tie. It is a
       property of the Hough threshold as much as of the paint -- rescaling one
       frame flips hash rows to solid -- so it may narrow a choice the
       observation left open, and may never overrule it.

    Returns ``(row_y_values, evidence)`` or ``None`` when the choice is still
    open. Refusing is the cheap failure; guessing is the expensive one.
    """
    rows_y = (
        tmpl.sideline_y_south,
        tmpl.hash_y_south,
        tmpl.hash_y_north,
        tmpl.sideline_y_north,
    )
    sideline_rows = {0, len(rows_y) - 1}
    allowed = {
        ROW_SIDELINE: sideline_rows,
        ROW_INTERIOR: set(range(len(rows_y))) - sideline_rows,
    }

    def observed(combo: tuple[int, ...]) -> bool:
        for i, verdict in enumerate(verdicts or []):
            if i < len(combo) and verdict in allowed and combo[i] not in allowed[verdict]:
                return False
        return True

    candidates = [
        combo
        for combo in itertools.combinations(range(len(rows_y)), len(dashed_pattern))
        if observed(combo)
    ]
    if len(candidates) == 1:
        return [float(rows_y[i]) for i in candidates[0]], "rows_observed"

    narrowed = [
        combo
        for combo in candidates
        if [_ROW_IS_DASHED[i] for i in combo] == dashed_pattern
    ]
    if len(narrowed) == 1:
        return [float(rows_y[i]) for i in narrowed[0]], "row_identity_unverified"
    return None


#: How far the across-field and down-field scales may differ before the
#: labelling is rejected. Perspective genuinely stretches one axis against the
#: other -- a low camera looking down the field compresses the far yard lines
#: hard -- so this is deliberately loose. It is not a calibration check; it is
#: an absurdity check.
MAX_SCALE_ANISOTROPY = 25.0


def _scale_is_plausible(src: list[list[float]], dst: list[list[float]]) -> bool:
    """Does the labelling imply a physically possible pair of scales?

    A mislabelling is not a small error. Two rows 22 px apart called the two
    sidelines assert that 53.3 yd of field fits in 22 px, while the yard lines
    in the same frame put 5 yd in a couple of hundred -- a scale disagreement of
    nearly fifty times, from a fit that satisfies RANSAC perfectly because it is
    self-consistent. Comparing the two directions *within one image* catches
    that without knowing anything about the camera, which is the point: there is
    no fixed height, angle or resolution to check against.
    """
    s = np.asarray(src, dtype=np.float64)
    d = np.asarray(dst, dtype=np.float64)
    scales: list[float] = []
    for axis in (0, 1):
        # Pairs separated along one field axis only, so each ratio measures a
        # single direction rather than a diagonal.
        other = 1 - axis
        for i, j in itertools.combinations(range(len(d)), 2):
            if abs(d[i][other] - d[j][other]) > 1e-6:
                continue
            field_gap = abs(d[i][axis] - d[j][axis])
            pixel_gap = float(np.linalg.norm(s[i] - s[j]))
            if field_gap > 1e-6 and pixel_gap > 1e-6:
                scales.append(field_gap / pixel_gap)
    if not scales:
        return True  # nothing to compare; not this check's call to make
    lo, hi = min(scales), max(scales)
    return hi <= lo * MAX_SCALE_ANISOTROPY


def build_correspondences(
    lines: list[tuple[float, float]],
    frame_shape: tuple[int, int],
    template: FieldTemplate | None = None,
    *,
    dashed_lines: list[tuple[float, float]] | None = None,
    touchlines: list[tuple[float, float]] | None = None,
    boundary: Any = None,
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
    verdicts = _row_verdicts(cross_ordered, boundary, (h, w), touchlines)
    matched = _match_rows_to_template([ln[2] for ln in cross_ordered], tmpl, verdicts)
    if matched is None:
        return _empty("ambiguous_field_rows")
    chosen_rows, evidence = matched
    reason_codes.append(evidence)

    # Which *particular* hash or sideline each row is remains open, and no
    # amount of boundary evidence closes it: the field is genuinely symmetric
    # under a 180° rotation, so both ends look alike and both sidelines look
    # alike. Lengths, separations and speeds are invariant under that rotation;
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
    elif not _scale_is_plausible(src, dst):
        return _empty("implausible_row_scale")

    return KeypointResult(
        src_pts=np.asarray(src, dtype=np.float64) if src else np.empty((0, 2)),
        dst_pts=np.asarray(dst, dtype=np.float64) if dst else np.empty((0, 2)),
        line_count=len(lines),
        field_coverage=0.0,
        yardline_angles=yardline_angles,
        reason_codes=reason_codes,
    )


def detect_keypoints(
    frame: np.ndarray,
    template: FieldTemplate | None = None,
    *,
    boundary: Any = None,
    grass: tuple[np.ndarray, float] | None = None,
) -> KeypointResult:
    """Full single-frame detection: masks → Hough → cluster → correspondences.

    Requires OpenCV. Failures degrade to an empty :class:`KeypointResult`
    with a reason code rather than raising, so the calibrate stage can record
    ``analytics_safe=False`` instead of crashing the pipeline.

    ``boundary`` and ``grass`` let a caller that has already computed them hand
    them in. The calibrate stage samples the same frames for its own boundary
    diagnostics, so without this the grass threshold runs three times per frame
    and the contour pass twice.
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

    grass_mask_, coverage = grass if grass is not None else grass_mask(frame)
    reason_codes: list[str] = []
    if coverage < 0.25:
        reason_codes.append("low_field_coverage")
    paint = white_paint_mask(frame, grass_mask_)
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

    # Where the surface ends is what identifies a row, so the boundary is not
    # optional enrichment here -- without it the labelling falls back to the
    # unreliable solid/dashed tag. Reuses the grass mask already in hand.
    if boundary is None:
        from pipeline.homography.field_boundary import detect_field_boundary

        boundary = detect_field_boundary(frame, grass=(grass_mask_, coverage))
    touchlines = boundary.touchlines() if boundary is not None else []

    result = build_correspondences(
        lines,
        frame.shape[:2],
        template,
        dashed_lines=dashed,
        touchlines=touchlines,
        boundary=boundary,
    )
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
