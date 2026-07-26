"""Resolve which way round the fitted field frame is.

A homography fitted to painted lines is only ever determined up to the
*symmetries of the paint*. The markings of a football field are unchanged by

* ``flip_x``  — ``(x, y) → (100 - x, y)``: the yard lines are the same set
  read from the other goal line,
* ``flip_y``  — ``(x, y) → (x, -y)``: the hash rows and sidelines are the same
  set read from the other sideline,
* ``rot180``  — the composition of the two.

Four labellings, one set of observed lines, identical re-projection error. The
calibrate stage picks one of the four by the order it happened to walk the
detected lines in, and nothing downstream can tell which it got: the DLT fits a
mirrored labelling exactly as well as the true one, and reports the same inlier
ratio.

This module closes that gap in two steps, because the four are not equally
wrong and they are not refuted by the same kind of evidence.

**Step 1 — handedness rules out the two reflections, from geometry alone.**
``flip_x`` and ``flip_y`` are orientation-*reversing*; ``rot180`` is not. A real
camera sits above the playing surface, and the image→ground map of a camera on
a fixed side of a plane has a fixed handedness. So the sign of the projective
Jacobian is an invariant, and a fit carrying the wrong sign describes a camera
*underneath the field*. That is checkable in three lines and certain — see
:func:`is_mirrored`. On the 29 clips of Toledo practice film that fit at all,
**15 (52%) came out mirrored**, which is to say better than half of all
calibrations had left and right swapped and nothing said so.

A mirrored fit is repairable rather than fatal: composing it with a field-frame
mirror restores the handedness and re-labels the same observed lines onto the
same landmark set, so the re-projection error is untouched. Which mirror is
used does not matter — ``flip_x`` and ``flip_y`` differ by ``rot180``, and
``rot180`` is exactly what step 1 cannot resolve.

**Step 2 — the remaining bit is not in the paint at all.** After step 1 the
ambiguity is precisely ``{identity, rot180}``: one bit. No geometric evidence
closes it, because the field really is symmetric under that rotation — lengths,
separations, speeds and hash spacings are all invariant. Only the *football*
breaks the tie, so :func:`estimate_attack_direction` reads it off the players:
at a set formation the defence has a secondary and the offence does not, so the
side of the line of scrimmage with the deeper players is the side being
attacked.

Measured on the same footage, with ground truth read off the film: comparing
the mean depth of the three deepest players on each side of the LOS calls the
direction correctly on **13 of 13** single set formations where the two sides
differ by at least 1.5 yd, and abstains on the rest. Voting that per-frame call
across the clip (:func:`estimate_over_frames`) resolves **27 of 29** clips and
is correct on all 27. The statistic the pipeline used before this — the mean
field-x displacement of every tracklet over its whole span — scored **10 of
18**, which on a two-way call is a coin toss.

What this does *not* settle: which particular end zone the offence is attacking
in absolute terms. Both the reflection and the template's yard-line offset are
resolved only relative to the clip's own fit, so ``los_x = 30`` still cannot be
read as "own 30". That needs a landmark the paint does not carry — numeral OCR
or an end-zone detection — and is out of scope here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "CAMERA_ABOVE_JACOBIAN_SIGN",
    "CONSENSUS_FRACTION",
    "DEPTH_MARGIN_YD",
    "OrientationEstimate",
    "estimate_attack_direction",
    "estimate_over_frames",
    "is_mirrored",
    "jacobian_sign",
    "unmirror",
]

# Sign of the image→field projective Jacobian for a camera above the playing
# surface. Verified against synthetic pinhole cameras over a sweep of position,
# yaw and pitch: every camera above the plane produces a negative determinant
# and every camera below produces a positive one. It depends only on the image
# convention (v increasing downward) and the template's axis order, so it is a
# constant of the coordinate system rather than a property of any venue.
CAMERA_ABOVE_JACOBIAN_SIGN: float = -1.0

# How much deeper one side of the LOS must be before the depth asymmetry is
# allowed to call the direction. The real separation is far larger than this --
# an offensive backfield runs to roughly 7 yd, a secondary sits at 10-15 -- so
# this is not a tuned decision boundary but a floor below which the frame has
# plainly cropped the deep defenders and the comparison means nothing.
DEPTH_MARGIN_YD: float = 1.5

# Players averaged into each side's depth. Three covers a two-high safety look
# plus a corner off the line; taking only the single deepest hands the call to
# one stray box, and taking every player dilutes the secondary into the front.
DEEPEST_N: int = 3

# Below this a "side" is a detection artifact, not a formation.
MIN_PER_SIDE: int = 3

# Share of the frames that resolved which must agree before a clip is called.
# Every threshold from a bare majority to 0.9 is right on all 29 clips measured,
# so the footage does not choose this number and it is not tuned to it: two
# thirds is the point at which a handful of badly framed frames can no longer
# outvote the rest, and a bare majority of noisy per-frame calls is not evidence.
CONSENSUS_FRACTION: float = 2.0 / 3.0

# Fewer resolved frames than this is one lucky formation, not a clip-level
# reading, however lopsided the vote looks.
MIN_RESOLVED_FRAMES: int = 3

# LOS search, matched to ``events.los_estimator`` so the two agree on where the
# line is: players within this many yards of each other in x form one cluster,
# and a cluster needs this many players to count as the line.
_LOS_EPS_YD: float = 2.0
_LOS_MIN_SAMPLES: int = 5

# Players nearer the LOS than this belong to neither backfield; they are the
# line itself and are excluded from the side split.
_LINE_HALF_WIDTH_YD: float = 0.5


# ── Step 1: handedness ────────────────────────────────────────────────────────


def jacobian_sign(H: np.ndarray, frame_shape: tuple[int, ...]) -> float | None:
    """Sign of the image→field Jacobian determinant at the frame centre.

    For a projective map the local Jacobian determinant is ``det(H) / w³``,
    where ``w`` is the homogeneous denominator at the point. The ``w³`` term is
    why this is not simply ``sign(det(H))``: a fit can carry a negative
    denominator over the visible image and invert the apparent handedness.

    Returns ``None`` when the centre lands on the horizon line, where the
    projection -- and so the question -- is undefined.
    """
    h, w_img = frame_shape[:2]
    u, v = w_img / 2.0, h / 2.0
    denom = float(H[2, 0] * u + H[2, 1] * v + H[2, 2])
    if abs(denom) < 1e-12:
        return None
    det = float(np.linalg.det(np.asarray(H, dtype=np.float64)))
    if not math.isfinite(det) or det == 0.0:
        return None
    return math.copysign(1.0, det / denom**3)


def is_mirrored(H: np.ndarray, frame_shape: tuple[int, ...]) -> bool:
    """Is this fit a mirror image of the field -- a camera below the ground?

    ``False`` when the question is undefined (degenerate fit, centre on the
    horizon): an unanswerable check must not manufacture a correction.
    """
    sign = jacobian_sign(H, frame_shape)
    if sign is None:
        return False
    return sign != CAMERA_ABOVE_JACOBIAN_SIGN


def unmirror(H: np.ndarray) -> np.ndarray:
    """Return the handedness-corrected twin of a mirrored fit.

    Composes the field-frame reflection ``y → -y``, which maps the template's
    landmark rows onto themselves (``±13.33`` swap, ``±26.665`` swap) and the
    yard lines onto themselves. The corrected fit therefore explains exactly the
    same observed lines with exactly the same re-projection error -- it is a
    relabelling, not a refit.

    ``flip_x`` would do equally well. The two differ by ``rot180``, which is the
    bit this step cannot resolve and :func:`estimate_attack_direction` can.
    """
    mirror = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]])
    return mirror @ np.asarray(H, dtype=np.float64)


# ── Step 2: the attacking direction, from the players ─────────────────────────


@dataclass(frozen=True)
class OrientationEstimate:
    """Which way the offence attacks, and how well that is known.

    ``direction`` is ``+1`` when the offence advances toward increasing field-x,
    ``-1`` toward decreasing, and ``None`` when the evidence did not separate.
    ``None`` is a real answer and callers must handle it: the alternative is the
    silent ``+1`` default this replaces, which was right about half the time and
    said so never.
    """

    direction: int | None
    evidence: str
    margin_yd: float | None = None
    los_x: float | None = None
    support: tuple[int, int] = (0, 0)
    frames_resolved: int = 0
    agreement: float | None = None

    @property
    def resolved(self) -> bool:
        return self.direction is not None

    def as_dict(self) -> dict[str, object]:
        """Diagnostics shape written to labels and calibration records."""
        return {
            "direction": self.direction,
            "evidence": self.evidence,
            "margin_yd": self.margin_yd,
            "los_x": None if self.los_x is None else round(self.los_x, 2),
            "frames_resolved": self.frames_resolved,
            "agreement": self.agreement,
        }


def _dense_cluster_centre(xs: np.ndarray) -> float | None:
    """Centre of the tightest dense 1-D cluster -- the line of scrimmage.

    Same single sorted sweep as ``events.los_estimator``: split on gaps wider
    than eps, keep runs of at least min_samples, take the one with the smallest
    spread. At a set formation that run is the two lines pressed together.
    """
    if len(xs) < _LOS_MIN_SAMPLES:
        return None
    order = np.sort(np.asarray(xs, dtype=np.float64))
    clusters: list[list[float]] = []
    current: list[float] = [float(order[0])]
    for prev, value in zip(order[:-1], order[1:]):
        if value - prev <= _LOS_EPS_YD:
            current.append(float(value))
        else:
            clusters.append(current)
            current = [float(value)]
    clusters.append(current)
    dense = [c for c in clusters if len(c) >= _LOS_MIN_SAMPLES]
    if not dense:
        return None
    tightest = min(dense, key=lambda c: float(np.std(c)))
    return float(np.mean(tightest))


def _mean_deepest(depths: np.ndarray) -> float:
    return float(np.mean(np.sort(depths)[::-1][:DEEPEST_N]))


def estimate_attack_direction(
    positions: list[tuple[float, float]] | np.ndarray,
    *,
    los_x: float | None = None,
) -> OrientationEstimate:
    """Call the offence's attacking direction from one set formation.

    ``positions`` are player field coordinates ``(x, y)`` in template yards at a
    frame where the teams are aligned -- the snap, or the quietest frame before
    it. Officials and sideline figures must already be gone; this reads the
    depth of the deepest players and a coach standing behind the huddle answers
    to the same description as a free safety.

    The rule is the one asymmetry of an aligned football formation that does not
    depend on how the clip was shot: **the defence fields a secondary and the
    offence does not.** Both teams put a line on the ball; only one of them
    keeps players 10+ yards off it on a snap-to-snap basis. So the deeper side
    is the defence, and the offence attacks toward it.

    Refuses -- ``direction=None`` -- rather than guessing, whenever the two
    sides fail to separate by :data:`DEPTH_MARGIN_YD`. On this footage that is
    usually the drone framing tight enough to crop the safeties out, in which
    case the statistic is comparing two offensive backfields and has nothing to
    say.
    """
    pts = np.asarray(positions, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2 * MIN_PER_SIDE:
        return OrientationEstimate(None, "insufficient_players")

    xs = pts[:, 0]
    line_x = los_x if los_x is not None else _dense_cluster_centre(xs)
    if line_x is None:
        return OrientationEstimate(None, "no_line_of_scrimmage")

    below = pts[xs < line_x - _LINE_HALF_WIDTH_YD]
    above = pts[xs > line_x + _LINE_HALF_WIDTH_YD]
    if len(below) < MIN_PER_SIDE or len(above) < MIN_PER_SIDE:
        # One side of the ball is missing. Nothing here is comparable, and the
        # deeper-side rule would be reading a single team against itself.
        return OrientationEstimate(
            None,
            "one_sided_formation",
            los_x=float(line_x),
            support=(len(below), len(above)),
        )

    depth_below = _mean_deepest(np.abs(below[:, 0] - line_x))
    depth_above = _mean_deepest(np.abs(above[:, 0] - line_x))
    margin = abs(depth_above - depth_below)
    support = (len(below), len(above))

    if margin < DEPTH_MARGIN_YD:
        return OrientationEstimate(
            None,
            "depth_symmetric",
            margin_yd=round(margin, 2),
            los_x=float(line_x),
            support=support,
        )

    direction = 1 if depth_above > depth_below else -1
    return OrientationEstimate(
        direction,
        "secondary_depth",
        margin_yd=round(margin, 2),
        los_x=float(line_x),
        support=support,
        frames_resolved=1,
        agreement=1.0,
    )


def estimate_over_frames(
    frames: list[list[tuple[float, float]]] | list[np.ndarray],
) -> OrientationEstimate:
    """Call the clip by voting :func:`estimate_attack_direction` over frames.

    One frame is a weak reading: on this footage a single set formation resolves
    only 13 of 29 clips, because the drone is forever cropping one team's
    depth, and a tight frame produces not a wrong answer but no answer. Frames
    are cheap and their failures are largely independent, so the clip is decided
    by the frames that *did* separate.

    Voting is over every supplied frame rather than a window around the snap.
    That is deliberate: it needs no snap detection -- ``stage_labels`` sees
    ``snap_frame = 0`` whenever the events stage found no snap -- and after the
    snap the asymmetry that drives the statistic does not vanish, since the
    secondary stays behind the receivers it is covering. Frames where the
    formation has genuinely dissolved fail the per-frame margin gate and drop
    out on their own rather than voting noise.

    Requires :data:`CONSENSUS_FRACTION` of the resolved frames to agree, so a
    clip whose frames genuinely disagree is refused rather than settled by a
    one-vote margin. Across 29 clips this resolves 27 and is correct on all 27.
    """
    votes: list[int] = []
    last: OrientationEstimate | None = None
    for positions in frames:
        estimate = estimate_attack_direction(positions)
        if estimate.resolved and estimate.direction is not None:
            votes.append(estimate.direction)
            last = estimate

    if len(votes) < MIN_RESOLVED_FRAMES:
        return OrientationEstimate(
            None, "insufficient_resolved_frames", frames_resolved=len(votes)
        )

    forward = sum(1 for v in votes if v > 0)
    backward = len(votes) - forward
    winner = max(forward, backward)
    agreement = round(winner / len(votes), 3)
    if forward == backward or agreement < CONSENSUS_FRACTION:
        return OrientationEstimate(
            None,
            "frames_disagree",
            frames_resolved=len(votes),
            agreement=agreement,
        )

    return OrientationEstimate(
        1 if forward > backward else -1,
        "secondary_depth_vote",
        margin_yd=last.margin_yd if last is not None else None,
        los_x=last.los_x if last is not None else None,
        frames_resolved=len(votes),
        agreement=agreement,
    )
