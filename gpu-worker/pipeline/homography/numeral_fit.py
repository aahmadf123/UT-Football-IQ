"""Field registration anchored on the painted yard numbers.

Every NCAA field paints its yard numbers (10-50) in two rows, centred on
their yard line with the tops 9 yd from the sideline. A detector finds them
(``pipeline.field_model``); each one names an absolute field position, so no
camera setup or per-venue calibration is needed, and the camera may pan and
zoom freely.

The fit is guided: a first homography from the numerals alone is used to
recognise every other painted line in the frame (yard lines every 5 yd, hash
rows, sidelines), and their intersections refine it. This works the same
whether the yard lines or the hash rows dominate the image -- the failure of
the classical detector on elevated game sidelines, which assumes the drone's
geometry (``yardline_keypoints.build_correspondences``).

Conventions match :mod:`field_template`: x 0-100 yd goal line to goal line,
y -26.665 (south) to +26.665 (north). The near numeral row is labelled south
and image-left of the 50 is low x; the 180-degree ambiguity that leaves is
resolved downstream by ``field_orientation``, as for the classical fit.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any

import numpy as np

from pipeline.homography import field_orientation as fo
from pipeline.homography import field_template as ft
from pipeline.homography import yardline_keypoints as yk
from pipeline.homography.project import projects_onto_field

#: Detections below this confidence are ignored.
MIN_NUMERAL_CONF = 0.25
#: Inlier distance for the guided refit, in yards.
REFIT_THRESHOLD_YD = 0.5
#: A painted line is recognised when its projection stays this close (yards)
#: to a template line along its whole visible length.
LINE_TOL_YD = 0.9
MIN_INLIERS = 6
#: The numerals are the only absolute evidence: a fit that moves them off
#: their own yard line has snapped every line to its neighbour (a 5-yard
#: shift that is otherwise perfectly self-consistent).
MAX_NUMERAL_ERROR_YD = 1.5
MAX_RMS_YD = 0.6
ROWS_Y = (-ft.HALF_WIDTH, ft.HASH_Y_SOUTH, ft.HASH_Y_NORTH, ft.HALF_WIDTH)
YARD_X = tuple(range(0, 101, 5))


@dataclass(frozen=True)
class Numeral:
    name: str
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 (pixels)
    x_yd: float
    near: bool


def class_to_field(name: str) -> tuple[float, bool] | None:
    """``(x_yd, near_row)`` for a detector class such as ``nl3`` or ``f5``."""
    if len(name) < 2 or name[0] not in "nf":
        return None
    near = name[0] == "n"
    if name[1:] == "5":
        return 50.0, near
    if len(name) != 3 or name[1] not in "lr" or name[2] not in "1234":
        return None
    tens = 10.0 * int(name[2])
    return (tens if name[1] == "l" else 100.0 - tens), near


def numerals_from_detections(
    detections: Sequence[tuple[str, float, Sequence[float]]],
) -> list[Numeral]:
    """Confident numerals, one per painted number.

    Two detections of the same box (the detector unsure between "30" and
    "40") collapse to the more confident one. Repeated classes elsewhere in
    the frame are kept: a misread digit is fixed later by
    :func:`labelling_hypotheses`, but a dropped number is just lost.
    """
    kept: list[Numeral] = []
    candidates = sorted(detections, key=lambda d: -float(d[1]))
    for name, conf, box in candidates:
        mapped = class_to_field(name)
        if mapped is None or conf < MIN_NUMERAL_CONF:
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        if any(_iou((x1, y1, x2, y2), n.box) > 0.4 for n in kept):
            continue
        kept.append(Numeral(name, float(conf), (x1, y1, x2, y2), mapped[0], mapped[1]))
    return kept


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def numeral_correspondences(numerals: Sequence[Numeral]) -> tuple[np.ndarray, np.ndarray]:
    """Pixel/field pairs: each numeral's box centre is on its yard line, 8 yd
    in from its sideline. (Only the centre: a box's top and bottom share its
    centre's image column, which would wrongly force yard lines vertical.)"""
    src = [[(n.box[0] + n.box[2]) / 2.0, (n.box[1] + n.box[3]) / 2.0] for n in numerals]
    dst = [[n.x_yd, ft.NUMERAL_Y_SOUTH if n.near else ft.NUMERAL_Y_NORTH] for n in numerals]
    return np.asarray(src, dtype=float).reshape(-1, 2), np.asarray(dst, dtype=float).reshape(-1, 2)


def _row_step_hypotheses(row: Sequence[Numeral]) -> list[list[int]]:
    """Retain neighboring integer spacings when a pixel gap is ambiguous."""
    centres = [(n.box[0] + n.box[2]) / 2.0 for n in row]
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    typical = float(np.median(gaps)) if gaps else 1.0
    choices = []
    for gap in gaps:
        ratio = gap / typical if typical > 0 else 1.0
        low, high = max(1, math.floor(ratio)), max(1, math.ceil(ratio))
        choices.append(sorted({low, high}))
    patterns = []
    for increments in product(*choices):
        steps = [0]
        for increment in increments:
            steps.append(steps[-1] + increment)
        patterns.append(steps)
    return patterns or [[0]]


def _on_row(row: list[Numeral]) -> list[Numeral]:
    """Drop detections that don't sit on their row's line (a logo or a
    player's number read as a yard number), worst first."""
    kept = list(row)
    while len(kept) >= 3:
        centres = np.asarray([[(n.box[0] + n.box[2]) / 2, (n.box[1] + n.box[3]) / 2] for n in kept])
        mean = centres.mean(axis=0)
        normal = np.linalg.svd(centres - mean)[2][1]
        residual = np.abs((centres - mean) @ normal)
        height = float(np.median([n.box[3] - n.box[1] for n in kept]))
        worst = int(np.argmax(residual))
        if residual[worst] <= 0.75 * height:
            break
        kept.pop(worst)
    return kept


def labelling_hypotheses(numerals: Sequence[Numeral], limit: int = 8) -> list[list[Numeral]]:
    """Plausible yard values for the detected numerals, best first.

    The detector reliably finds a numeral's row and position but can misread
    its digits ("20" as "40"). Along a row the numbers step by 10 yd in one
    direction, shared by both rows, so each detection's class is only a vote
    for where its row starts; every consistent labelling is returned, ranked
    by confidence-weighted agreement with the classes.
    """
    rows = {
        near: sorted(
            _on_row([n for n in numerals if n.near == near]), key=lambda n: n.box[0] + n.box[2]
        )
        for near in (True, False)
    }
    scored: list[tuple[float, list[Numeral]]] = []
    for direction in (1, -1):
        options: dict[bool, list[tuple[float, list[float]]]] = {}
        for near, row in rows.items():
            if not row:
                options[near] = [(0.0, [])]
                continue
            candidates: dict[tuple[float, ...], float] = {}
            for steps in _row_step_hypotheses(row):
                votes: dict[float, float] = {}
                for n, step in zip(row, steps):
                    start = n.x_yd - 10.0 * direction * step
                    votes[start] = votes.get(start, 0.0) + n.confidence
                for start, vote in sorted(votes.items(), key=lambda kv: -kv[1])[:3]:
                    xs = tuple(start + 10.0 * direction * step for step in steps)
                    if all(10.0 <= x <= 90.0 for x in xs):
                        candidates[xs] = max(vote, candidates.get(xs, 0.0))
            class_assignment = tuple(n.x_yd for n in row)
            if all(10.0 <= x <= 90.0 for x in class_assignment):
                candidates[class_assignment] = max(
                    sum(n.confidence for n in row), candidates.get(class_assignment, 0.0)
                )
            ranked = sorted(
                ((vote, list(xs)) for xs, vote in candidates.items()),
                key=lambda item: -item[0],
            )
            selected = ranked[:8]
            if class_assignment in candidates and not any(
                xs == list(class_assignment) for _, xs in selected
            ):
                selected[-1:] = [(candidates[class_assignment], list(class_assignment))]
            options[near] = selected or [(0.0, [])]
        for near_vote, near_xs in options[True]:
            for far_vote, far_xs in options[False]:
                relabelled = [
                    Numeral(n.name, n.confidence, n.box, x, n.near)
                    for n, x in [*zip(rows[True], near_xs), *zip(rows[False], far_xs)]
                ]
                if relabelled:
                    scored.append((near_vote + far_vote, relabelled))
    scored.sort(key=lambda item: -item[0])
    return [labelled for _, labelled in scored[:limit]]


def _line_through(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float]:
    """``(rho, theta)`` of the line through two points."""
    theta = math.atan2(q[0] - p[0], -(q[1] - p[1]))  # normal angle
    rho = p[0] * math.cos(theta) + p[1] * math.sin(theta)
    return rho, theta


def _distance(line: tuple[float, float], point: tuple[float, float]) -> float:
    rho, theta = line
    return abs(point[0] * math.cos(theta) + point[1] * math.sin(theta) - rho)


def _angle_gap(a: float, b: float) -> float:
    d = abs(a - b) % math.pi
    return min(d, math.pi - d)


def single_row_seeds(
    numerals: Sequence[Numeral], lines: Sequence[tuple[float, float]]
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Seed correspondences when only one numeral row is in frame.

    A tight broadcast shot often shows one row. Each numeral's yard line is
    the painted line through its centre; the row itself is the line through
    the numeral centres; and every roughly parallel line is a candidate hash
    row or sideline. Each candidate gives four points (two yard lines x two
    rows); the caller keeps whichever explains the most painted lines.
    """
    for near in (True, False):
        row = sorted((n for n in numerals if n.near == near), key=lambda n: n.box[0])
        if len(row) >= 2:
            break
    else:
        return []
    first, last = row[0], row[-1]
    centre = lambda n: ((n.box[0] + n.box[2]) / 2.0, (n.box[1] + n.box[3]) / 2.0)  # noqa: E731
    row_line = _line_through(centre(first), centre(last))
    yard_lines = []
    for n in (first, last):
        width = n.box[2] - n.box[0]
        through = [
            ln
            for ln in lines
            if _distance(ln, centre(n)) < 0.25 * width and _angle_gap(ln[1], row_line[1]) > 0.35
        ]
        if not through:
            return []
        yard_lines.append(min(through, key=lambda ln: _distance(ln, centre(n))))
    height = max(n.box[3] - n.box[1] for n in row)
    candidates: list[tuple[float, float]] = []
    for ln in lines:
        if _angle_gap(ln[1], row_line[1]) > 0.35:
            continue
        if min(_distance(ln, centre(n)) for n in row) < 1.5 * height:
            continue
        if all(abs(ln[0] - c[0]) > 4.0 or _angle_gap(ln[1], c[1]) > 0.03 for c in candidates):
            candidates.append(ln)
    numeral_y = ft.NUMERAL_Y_SOUTH if near else ft.NUMERAL_Y_NORTH
    sign = -1.0 if near else 1.0
    row_labels = (sign * abs(ft.HASH_Y_NORTH), sign * ft.HALF_WIDTH)
    seeds = []
    for cand in candidates[:12]:
        for label in row_labels:
            src, dst = [], []
            for n, yard in zip((first, last), yard_lines):
                for line, y in ((row_line, numeral_y), (cand, label)):
                    pt = yk._intersect(yard, line)
                    if pt is not None:
                        src.append(list(pt))
                        dst.append([n.x_yd, y])
            if len(src) == 4:
                seeds.append((np.asarray(src, dtype=float), np.asarray(dst, dtype=float)))
    return seeds


def _initial_geometry_ok(numerals: Sequence[Numeral]) -> bool:
    """Four numerals spanning both rows and two yard lines pin a homography."""
    near = {n.x_yd for n in numerals if n.near}
    far = {n.x_yd for n in numerals if not n.near}
    return len(numerals) >= 4 and len(near) >= 2 and len(far) >= 2


def _line_samples(rho: float, theta: float, w: int, h: int, n: int = 9) -> np.ndarray:
    """Points along a ``(rho, theta)`` line clipped to the frame."""
    c, s = math.cos(theta), math.sin(theta)
    ends: list[tuple[float, float]] = []
    if abs(s) > 1e-6:
        for x in (0.0, float(w - 1)):
            y = (rho - x * c) / s
            if 0 <= y <= h - 1:
                ends.append((x, y))
    if abs(c) > 1e-6:
        for y in (0.0, float(h - 1)):
            x = (rho - y * s) / c
            if 0 <= x <= w - 1:
                ends.append((x, y))
    if len(ends) < 2:
        return np.empty((0, 2))
    (ax, ay), (bx, by) = (
        ends[0],
        max(ends[1:], key=lambda p: math.hypot(p[0] - ends[0][0], p[1] - ends[0][1])),
    )
    t = np.linspace(0.05, 0.95, n)
    return np.stack([ax + t * (bx - ax), ay + t * (by - ay)], axis=1)


_SAMPLES = 9
LineSamples = tuple[np.ndarray, np.ndarray]


def line_samples(lines: Sequence[tuple[float, float]], frame_shape: tuple[int, ...]) -> LineSamples:
    """Sample points of every line, stacked ``(n_lines, _SAMPLES, 2)``, plus
    the index of each line with a visible span. Computed once per frame: the
    points don't depend on the homography being tested."""
    h, w = frame_shape[:2]
    pts, keep = [], []
    for i, (rho, theta) in enumerate(lines):
        sample = _line_samples(rho, theta, w, h, _SAMPLES)
        if len(sample):
            pts.append(sample)
            keep.append(i)
    return np.asarray(pts, dtype=float).reshape(-1, _SAMPLES, 2), np.asarray(keep, dtype=int)


def label_lines(
    lines: Sequence[tuple[float, float]],
    H: np.ndarray,
    frame_shape: tuple[int, ...],
    tolerance: float = LINE_TOL_YD,
    samples: LineSamples | None = None,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Recognise painted lines through ``H``: ``(rho, theta, x)`` yard lines
    and ``(rho, theta, y)`` rows (sidelines and hashes). Vectorised over all
    lines; Hough's duplicate votes per painted line collapse to the one that
    fits its template line best, so each field line contributes once."""
    points, index = samples if samples is not None else line_samples(lines, frame_shape)
    if len(index) == 0:
        return [], []
    field = _apply(H, points.reshape(-1, 2)).reshape(points.shape)
    finite = np.all(np.isfinite(field), axis=(1, 2))
    xs, ys = field[..., 0], field[..., 1]
    span_y = np.ptp(np.where(finite[:, None], ys, 0.0), axis=1)
    span_x = np.ptp(np.where(finite[:, None], xs, 0.0), axis=1)
    rows_y = np.asarray(ROWS_Y)

    best_yard: dict[float, tuple[float, float, float]] = {}
    best_row: dict[float, tuple[float, float, float]] = {}
    yard_mask = finite & (span_y > 3.0)
    if yard_mask.any():
        x_label = 5.0 * np.round(np.median(xs[yard_mask], axis=1) / 5.0)
        err = np.max(np.abs(xs[yard_mask] - x_label[:, None]), axis=1)
        for i, label, e in zip(index[yard_mask], x_label, err):
            if e <= tolerance and 0.0 <= label <= 100.0:
                if label not in best_yard or e < best_yard[label][0]:
                    best_yard[float(label)] = (float(e), *lines[i])
    row_mask = finite & ~(span_y > 3.0) & (span_x > 3.0)
    if row_mask.any():
        middle = np.median(ys[row_mask], axis=1)
        y_label = rows_y[np.argmin(np.abs(middle[:, None] - rows_y[None, :]), axis=1)]
        err = np.max(np.abs(ys[row_mask] - y_label[:, None]), axis=1)
        for i, label, e in zip(index[row_mask], y_label, err):
            if e <= tolerance and (label not in best_row or e < best_row[label][0]):
                best_row[float(label)] = (float(e), *lines[i])
    yard = [(r, t, x) for x, (_e, r, t) in sorted(best_yard.items())]
    rows = [(r, t, y) for y, (_e, r, t) in sorted(best_row.items())]
    return yard, rows


def _apply(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Vectorised pixel->field projection (inf where the point is at infinity)."""
    homogeneous = np.hstack([pts, np.ones((len(pts), 1))]) @ np.asarray(H, dtype=float).T
    with np.errstate(divide="ignore", invalid="ignore"):
        return homogeneous[:, :2] / homogeneous[:, 2:3]


def _intersections(
    yard: Sequence[tuple[float, float, float]],
    rows: Sequence[tuple[float, float, float]],
    frame_shape: tuple[int, ...],
) -> tuple[list[list[float]], list[list[float]]]:
    h, w = frame_shape[:2]
    src: list[list[float]] = []
    dst: list[list[float]] = []
    for yr, yt, x in yard:
        for rr, rt, y in rows:
            pt = yk._intersect((yr, yt), (rr, rt))
            if pt is not None and -0.05 * w <= pt[0] <= 1.05 * w and -0.05 * h <= pt[1] <= 1.05 * h:
                src.append([pt[0], pt[1]])
                dst.append([x, y])
    return src, dst


def _numeral_error(H: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    """Worst along-field distance (yd) of a numeral centre from its yard line."""
    if len(src) == 0:
        return math.inf
    projected = _apply(H, src)
    if not np.all(np.isfinite(projected)):
        return math.inf
    return float(np.max(np.abs(projected[:, 0] - dst[:, 0])))


def _ransac(
    src: np.ndarray, dst: np.ndarray, *, threshold: float
) -> tuple[np.ndarray | None, np.ndarray]:
    """Pixel->field homography by OpenCV's RANSAC (yards threshold), with an
    inlier mask. Many hypotheses are scored per anchor frame, so this needs
    the C++ solver rather than ``dlt_ransac``'s reference implementation."""
    import cv2

    src = np.asarray(src, dtype=np.float64).reshape(-1, 2)
    dst = np.asarray(dst, dtype=np.float64).reshape(-1, 2)
    if len(src) < 4:
        return None, np.zeros(len(src), dtype=bool)
    H, mask = cv2.findHomography(
        src, dst, cv2.RANSAC, ransacReprojThreshold=threshold, maxIters=2000, confidence=0.995
    )
    if H is None or not np.all(np.isfinite(H)):
        return None, np.zeros(len(src), dtype=bool)
    return H, mask.ravel().astype(bool)


def _rms(H: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.sum((_apply(H, src) - dst) ** 2, axis=1))))


@dataclass
class NumeralFit:
    homography: np.ndarray | None
    keypoints: yk.KeypointResult
    inlier_ratio: float
    rms_yd: float
    numerals: int
    reason_codes: list[str]


def fit_numerals(
    frame_shape: tuple[int, ...],
    numerals: Sequence[Numeral],
    lines: Sequence[tuple[float, float]],
) -> NumeralFit:
    """Fit pixel->field from numerals, refined by the lines they identify."""

    def failed(code: str, n_lines: int = len(lines)) -> NumeralFit:
        empty = yk.KeypointResult(
            src_pts=np.empty((0, 2)),
            dst_pts=np.empty((0, 2)),
            line_count=n_lines,
            field_coverage=0.0,
            reason_codes=[code],
        )
        return NumeralFit(None, empty, 0.0, math.inf, len(numerals), [code])

    samples = line_samples(lines, frame_shape)
    both_rows = _initial_geometry_ok(numerals)
    if (
        not both_rows
        and max(sum(1 for n in numerals if n.near), sum(1 for n in numerals if not n.near)) < 2
    ):
        return failed("insufficient_numerals")
    # Pick the labelling whose first fit explains the most painted lines.
    best: tuple[int, np.ndarray, np.ndarray, np.ndarray] | None = None
    for labelled in labelling_hypotheses(numerals):
        hyp_src, hyp_dst = numeral_correspondences(labelled)
        seeds = [(hyp_src, hyp_dst)] if both_rows else single_row_seeds(labelled, lines)
        for seed_src, seed_dst in seeds:
            candidate, _ = _ransac(seed_src, seed_dst, threshold=1.5)
            if candidate is None or not projects_onto_field(candidate, frame_shape):
                continue
            if _numeral_error(candidate, hyp_src, hyp_dst) > 2 * MAX_NUMERAL_ERROR_YD:
                continue
            yard_h, rows_h = label_lines(lines, candidate, frame_shape, 2.0 * LINE_TOL_YD, samples)
            support = len(yard_h) * max(1, len(rows_h))
            if best is None or support > best[0]:
                best = (support, candidate, hyp_src, hyp_dst)
    if best is None:
        return failed("numeral_fit_failed")
    _support, H, num_src, num_dst = best

    src: np.ndarray = num_src
    dst: np.ndarray = num_dst
    yard: list[tuple[float, float, float]] = []
    rows: list[tuple[float, float, float]] = []
    mask = np.ones(len(src), dtype=bool)
    for tolerance in (2.0 * LINE_TOL_YD, LINE_TOL_YD, LINE_TOL_YD):
        yard, rows = label_lines(lines, H, frame_shape, tolerance, samples)
        line_src, line_dst = _intersections(yard, rows, frame_shape)
        # Numeral centres stay in as absolute anchors.
        if len(line_src) >= 4:
            src = np.vstack([np.asarray(line_src), num_src])
            dst = np.vstack([np.asarray(line_dst), num_dst])
        refit, mask = _ransac(src, dst, threshold=REFIT_THRESHOLD_YD)
        if refit is None:
            break
        H = refit

    inliers = int(np.count_nonzero(mask))
    in_src, in_dst = src[mask], dst[mask]
    if inliers < MIN_INLIERS:
        return failed("low_inlier_ratio")
    rms = _rms(H, in_src, in_dst)
    codes = ["numeral_anchored"]
    if fo.is_mirrored(H, frame_shape):
        H = fo.unmirror(H)
        in_dst = in_dst * np.array([1.0, -1.0])
        codes.append("mirror_corrected")
    if rms > MAX_RMS_YD:
        return failed("numeral_fit_imprecise")
    if (
        _numeral_error(
            H, num_src, num_dst * ([1.0, -1.0] if "mirror_corrected" in codes else [1.0, 1.0])
        )
        > MAX_NUMERAL_ERROR_YD
    ):
        return failed("numeral_disagreement")
    if not projects_onto_field(H, frame_shape):
        return failed("projection_off_field")
    if len({x for *_, x in yard}) < 2 and len({n.near for n in numerals}) < 2:
        return failed("insufficient_structured_lines")
    if not yk._scale_is_plausible(in_src.tolist(), in_dst.tolist()):
        return failed("implausible_row_scale")
    keypoints = yk.KeypointResult(
        src_pts=in_src,
        dst_pts=in_dst,
        line_count=len(lines),
        field_coverage=1.0,
        yardline_angles=[t % math.pi for _, t, _ in yard],
        reason_codes=codes,
        fit_confidence=fit_confidence(inliers, rms),
    )
    return NumeralFit(H, keypoints, inliers / max(len(src), 1), rms, len(numerals), codes)


def fit_confidence(inliers: int, rms_yd: float) -> float:
    """Confidence for a numeral-anchored fit: tight residuals over many
    inliers. Crosses the 0.75 safety threshold around 10 inliers at 0.4 yd."""
    support = min(1.0, inliers / 12.0)
    precision = max(0.0, 1.0 - rms_yd / (2 * MAX_RMS_YD))
    return round(min(1.0, 0.45 * support + 0.45 * precision + 0.1 * min(support, precision)), 4)


def frame_lines(frame: np.ndarray) -> list[tuple[float, float]]:
    """Solid and dashed painted lines in a frame (the classical detector's)."""
    grass, _coverage = yk.grass_mask(frame)
    paint = yk.white_paint_mask(frame, grass)
    return yk.detect_hough_lines(paint) + yk.detect_dashed_lines(paint)


def make_frame_fitter(
    detect: Callable[[np.ndarray], list[tuple[str, float, Sequence[float]]]],
    fallback: Callable[[list[np.ndarray]], Any] | None = None,
) -> Callable[[list[np.ndarray]], Any]:
    """A ``fit_frame`` for ``homography.timeline.calibration_timeline``:
    best numeral fit over the frames, else ``fallback`` (the classical fit).
    Returns ``(H, KeypointResult, inlier_ratio, reason_codes)`` or ``None``."""

    def fit(frames: list[np.ndarray]) -> Any:
        best: NumeralFit | None = None
        for frame in frames:
            numerals = numerals_from_detections(detect(frame))
            if len(numerals) < 2:
                continue
            result = fit_numerals(frame.shape, numerals, frame_lines(frame))
            if result.homography is not None and (
                best is None or result.keypoints.fit_confidence > best.keypoints.fit_confidence
            ):
                best = result
        if best is not None:
            return best.homography, best.keypoints, best.inlier_ratio, best.reason_codes
        return fallback(frames) if fallback is not None else None

    return fit
