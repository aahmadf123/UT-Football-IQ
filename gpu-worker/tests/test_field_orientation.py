"""Tests for resolving the field template's orientation (task #12).

Two questions, two kinds of evidence: handedness is geometry and is certain,
the attacking direction is football and abstains when the players do not
settle it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from pipeline.homography import field_orientation as fo
from pipeline.homography.project import apply_homography

FRAME = (720, 1280)


# ── Synthetic cameras ─────────────────────────────────────────────────────────


def _camera(pos: tuple[float, float, float], yaw: float, pitch: float) -> np.ndarray:
    """image→field homography for a pinhole camera viewing the plane z = 0."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    fwd = np.array([cp * cy, cp * sy, -sp])
    right = np.cross(fwd, np.array([0.0, 0.0, 1.0]))
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    R = np.vstack([right, down, fwd])
    t = -R @ np.array(pos)
    K = np.array([[900.0, 0.0, 640.0], [0.0, 900.0, 360.0], [0.0, 0.0, 1.0]])
    P = K @ np.column_stack([R[:, 0], R[:, 1], t])
    return np.linalg.inv(P)


ABOVE = [
    ((0.0, 0.0, 40.0), 0.0, math.radians(45)),
    ((30.0, -20.0, 25.0), math.radians(37), math.radians(70)),
    ((-15.0, 35.0, 60.0), math.radians(143), math.radians(25)),
    ((5.0, 5.0, 12.0), math.radians(255), math.radians(89)),
]


@pytest.mark.parametrize("pos,yaw,pitch", ABOVE)
def test_camera_above_the_field_is_never_mirrored(pos, yaw, pitch) -> None:
    """The invariant the guard rests on, over a spread of real viewpoints."""
    H = _camera(pos, yaw, pitch)
    assert fo.jacobian_sign(H, FRAME) == fo.CAMERA_ABOVE_JACOBIAN_SIGN
    assert fo.is_mirrored(H, FRAME) is False


@pytest.mark.parametrize("pos,yaw,pitch", ABOVE)
def test_a_reflected_fit_is_detected(pos, yaw, pitch) -> None:
    """Mirroring the template is exactly what the paint cannot rule out."""
    flip_y = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]])
    flip_x = np.array([[-1.0, 0.0, 100.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    H = _camera(pos, yaw, pitch)
    for mirror in (flip_y, flip_x):
        assert fo.is_mirrored(mirror @ H, FRAME) is True


@pytest.mark.parametrize("pos,yaw,pitch", ABOVE)
def test_unmirror_restores_handedness(pos, yaw, pitch) -> None:
    H = _camera(pos, yaw, pitch)
    mirrored = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]]) @ H
    assert fo.is_mirrored(fo.unmirror(mirrored), FRAME) is False


def test_rot180_is_not_a_handedness_error() -> None:
    """The bit handedness leaves open, and must leave open.

    A fit rotated end-for-end describes a perfectly possible camera. If this
    check flagged it, step 1 would be silently answering step 2's question.
    """
    rot180 = np.array([[-1.0, 0.0, 100.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]])
    H = _camera((0.0, 0.0, 40.0), 0.0, math.radians(45))
    assert fo.is_mirrored(rot180 @ H, FRAME) is False


def test_unmirror_preserves_reprojection() -> None:
    """Correcting handedness re-labels the landmarks; it does not move a point.

    A yard line crossing the south hash lands on the north hash of the
    corrected fit — the same painted intersection, read off the other sideline.
    """
    H = _camera((10.0, -30.0, 35.0), math.radians(20), math.radians(50))
    mirrored = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]]) @ H
    fixed = fo.unmirror(mirrored)
    for px in ((400.0, 300.0), (900.0, 500.0), (640.0, 360.0)):
        x_bad, y_bad = apply_homography(mirrored, px)
        x_ok, y_ok = apply_homography(fixed, px)
        assert x_ok == pytest.approx(x_bad, abs=1e-9)
        assert y_ok == pytest.approx(-y_bad, abs=1e-9)


def test_degenerate_fit_is_not_called_mirrored() -> None:
    """An unanswerable check must not manufacture a correction."""
    assert fo.is_mirrored(np.zeros((3, 3)), FRAME) is False
    assert fo.jacobian_sign(np.zeros((3, 3)), FRAME) is None


# ── The attacking direction ───────────────────────────────────────────────────


def _formation(los: float, direction: int, *, secondary_depth: float = 12.0):
    """A plausible aligned formation with the defence on the attacked side."""
    pts: list[tuple[float, float]] = []
    for y in (-4.0, -2.0, 0.0, 2.0, 4.0):        # offensive line, on the ball
        pts.append((los - 0.2 * direction, y))
    pts.append((los - 5.0 * direction, 0.0))     # quarterback
    pts.append((los - 7.0 * direction, 2.0))     # back
    # Receivers a yard off the ball — the offence's whole depth is ~7 yd, which
    # is the point: it has no secondary to put behind that.
    pts.extend([(los - 1.0 * direction, -20.0), (los - 1.0 * direction, 20.0)])
    for y in (-3.0, 0.0, 3.0):                   # defensive front
        pts.append((los + 1.5 * direction, y))
    pts.append((los + 5.0 * direction, 0.0))     # linebacker
    for y in (-14.0, 14.0):                      # secondary — the asymmetry
        pts.append((los + secondary_depth * direction, y))
    return pts


@pytest.mark.parametrize("direction", (1, -1))
@pytest.mark.parametrize("los", (25.0, 50.0, 78.0))
def test_direction_from_secondary_depth(direction: int, los: float) -> None:
    est = fo.estimate_attack_direction(_formation(los, direction))
    assert est.direction == direction
    assert est.evidence == "secondary_depth"
    assert est.los_x == pytest.approx(los, abs=1.0)


def test_refuses_when_the_secondary_is_cropped() -> None:
    """A tight frame is the common case, and it is a refusal not a guess."""
    est = fo.estimate_attack_direction(_formation(50.0, 1, secondary_depth=5.0))
    assert est.direction is None
    assert est.evidence == "depth_symmetric"
    assert est.margin_yd is not None and est.margin_yd < fo.DEPTH_MARGIN_YD


def test_refuses_with_one_side_of_the_ball() -> None:
    est = fo.estimate_attack_direction(
        [(50.0, y) for y in range(-5, 5)] + [(56.0, 0.0)]
    )
    assert est.direction is None
    assert est.evidence in ("one_sided_formation", "no_line_of_scrimmage")


def test_refuses_with_too_few_players() -> None:
    est = fo.estimate_attack_direction([(50.0, 0.0), (52.0, 1.0)])
    assert est.direction is None
    assert est.evidence == "insufficient_players"


def test_no_line_of_scrimmage_when_players_are_scattered() -> None:
    scattered = [(10.0 * i, float(i)) for i in range(10)]
    est = fo.estimate_attack_direction(scattered)
    assert est.direction is None
    assert est.evidence == "no_line_of_scrimmage"


def test_supplied_los_is_used_over_the_cluster_search() -> None:
    est = fo.estimate_attack_direction(_formation(50.0, 1), los_x=50.0)
    assert est.direction == 1
    assert est.los_x == pytest.approx(50.0)


# ── Clip-level vote ───────────────────────────────────────────────────────────


def test_vote_carries_a_clip_past_unusable_frames() -> None:
    """Frames that resolve decide the clip; cropped ones drop out silently."""
    good = [_formation(50.0, -1) for _ in range(4)]
    cropped = [_formation(50.0, -1, secondary_depth=5.0) for _ in range(6)]
    est = fo.estimate_over_frames(cropped + good)
    assert est.direction == -1
    assert est.evidence == "secondary_depth_vote"
    assert est.frames_resolved == 4
    assert est.agreement == 1.0


def test_vote_refuses_when_frames_disagree() -> None:
    frames = [_formation(50.0, 1) for _ in range(5)]
    frames += [_formation(50.0, -1) for _ in range(4)]
    est = fo.estimate_over_frames(frames)
    assert est.direction is None
    assert est.evidence == "frames_disagree"
    assert est.agreement is not None and est.agreement < fo.CONSENSUS_FRACTION


def test_vote_refuses_on_a_bare_majority() -> None:
    """A one-vote margin over noisy per-frame calls is not evidence."""
    frames = [_formation(50.0, 1) for _ in range(5)]
    frames += [_formation(50.0, -1) for _ in range(4)]
    assert fo.estimate_over_frames(frames).direction is None


def test_vote_refuses_with_too_few_resolved_frames() -> None:
    frames = [_formation(50.0, 1)] + [
        _formation(50.0, 1, secondary_depth=5.0) for _ in range(20)
    ]
    est = fo.estimate_over_frames(frames)
    assert est.direction is None
    assert est.evidence == "insufficient_resolved_frames"
    assert est.frames_resolved == 1


def test_vote_on_no_frames_at_all() -> None:
    est = fo.estimate_over_frames([])
    assert est.direction is None
    assert est.resolved is False
    assert est.as_dict()["direction"] is None
