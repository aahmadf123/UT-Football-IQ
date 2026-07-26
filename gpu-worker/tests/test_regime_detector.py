"""Unit tests for the capture-regime detector (Issue #126).

Covers all four pixel-only features and the logistic-fusion path on five
synthetic "clips" built in memory. The tests deliberately avoid the
OpenCV-dependent ``_sample_frames`` helper — they feed the four features
straight into ``CaptureRegimeDetector.fuse(...)`` or call the feature
functions on hand-crafted numpy stacks. That keeps the suite
deterministic and runnable in any container that has numpy.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.homography import regime_detector as rd
from pipeline.homography.regime_detector import (
    DRONE_FOLLOW,
    FIXED_SIDELINE,
    UNCONSTRAINED,
    UNKNOWN,
    CaptureRegimeDetector,
    _framing_breakout_score,
    _global_affine_score,
    _static_background_score,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _solid_field_frame(h: int = 90, w: int = 160) -> np.ndarray:
    """A frame whose entire surface reads as field (green dominant)."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[..., 1] = 150  # G
    frame[..., 0] = 80  # B
    frame[..., 2] = 80  # R
    return frame


def _cropped_field_frame(
    h: int = 90, w: int = 160, crop_frac: float = 0.4
) -> np.ndarray:
    """Field-green only in the central region; left+right bands are non-field."""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    crop = int(w * crop_frac / 2)
    frame[:, crop : w - crop, 1] = 150
    frame[:, crop : w - crop, 0] = 80
    frame[:, crop : w - crop, 2] = 80
    # Non-field bands: blue-ish stands
    frame[:, :crop] = (180, 100, 100)
    frame[:, w - crop :] = (180, 100, 100)
    return frame


# ── Feature: static-background score ─────────────────────────────────────────


def test_static_background_score_fixed_camera_is_low():
    """Identical frames ⇒ deviation 0 ⇒ score 0 (fixed-camera signature)."""
    stack = np.stack([_solid_field_frame() for _ in range(8)], axis=0)
    assert _static_background_score(stack) == 0.0


def test_static_background_score_drone_pan_is_high():
    """Frames that drift across colors (a pan) ⇒ high deviation, score → 1."""
    frames = []
    for i in range(8):
        f = _solid_field_frame()
        f[..., 1] = (50 + i * 20) % 255  # ramp the green channel
        frames.append(f)
    score = _static_background_score(np.stack(frames, axis=0))
    assert score >= 0.5


# ── Feature: framing breakout score ──────────────────────────────────────────


def test_framing_breakout_score_field_reaches_edges():
    """Solid field touches both edges ⇒ low breakout score."""
    stack = np.stack([_solid_field_frame() for _ in range(4)], axis=0)
    assert _framing_breakout_score(stack) <= 0.05


def test_framing_breakout_score_field_cropped():
    """Field cropped away from both edges ⇒ score → 1."""
    stack = np.stack([_cropped_field_frame() for _ in range(4)], axis=0)
    score = _framing_breakout_score(stack)
    assert score >= 0.9


# ── Feature: global-affine optical flow proxy ────────────────────────────────


def test_global_affine_score_no_motion():
    """Identical frames ⇒ zero diff ⇒ zero global-affine signal."""
    stack = np.stack([_solid_field_frame() for _ in range(4)], axis=0)
    assert _global_affine_score(stack) == 0.0


def test_global_affine_score_camera_pan():
    """A uniform brightness ramp across frames mimics a camera pan and should
    register a non-trivial global-affine signal."""
    frames = []
    base = _solid_field_frame()
    for i in range(6):
        f = base.copy()
        # Shift the green channel by a constant amount across the whole frame
        f[..., 1] = np.clip(base[..., 1].astype(np.int32) + i * 20, 0, 255).astype(
            np.uint8
        )
        frames.append(f)
    stack = np.stack(frames, axis=0)
    score = _global_affine_score(stack)
    assert score > 0.3


# ── Feature: extract_features integrates ─────────────────────────────────────


def test_extract_features_returns_all_four_keys():
    stack = [_solid_field_frame() for _ in range(4)]
    feats = rd._extract_features(stack)
    assert set(feats.keys()) == {
        "static_bg_score",
        "vp_altitude_score",
        "global_affine_score",
        "framing_breakout_score",
    }
    for v in feats.values():
        assert 0.0 <= float(v) <= 1.0


# ── Fusion: logistic regression + thresholding ───────────────────────────────


def test_fuse_high_drone_signal_classifies_drone_follow():
    """All four features pegged high (drone signature) ⇒ drone_follow."""
    detector = CaptureRegimeDetector()
    features = {
        "static_bg_score": 1.0,
        "vp_altitude_score": 1.0,
        "global_affine_score": 1.0,
        "framing_breakout_score": 1.0,
    }
    result = detector.fuse(features)
    assert result.regime == DRONE_FOLLOW
    assert result.confidence > 0.5
    assert result.features == {k: 1.0 for k in features}


def test_fuse_low_drone_signal_classifies_fixed_sideline():
    """All four features pegged low (fixed signature) ⇒ fixed_sideline."""
    detector = CaptureRegimeDetector()
    features = {
        "static_bg_score": 0.0,
        "vp_altitude_score": 0.0,
        "global_affine_score": 0.0,
        "framing_breakout_score": 0.0,
    }
    result = detector.fuse(features)
    assert result.regime == FIXED_SIDELINE
    assert result.confidence > 0.5


def test_fuse_uncertain_signal_is_first_class_unconstrained():
    """Mid-range features ⇒ ``unconstrained`` (any-camera generic path).

    A confident non-match is not a failure (ADR 0005): footage that fits
    neither special regime takes the generic pipeline path. ``unknown`` is
    reserved for hard analysis failures (see the unsamplable-video test).
    """
    detector = CaptureRegimeDetector(margin=0.0)
    # Features chosen so logit lands near 0 → p≈0.5 → confidence ≈ 0
    features = {
        "static_bg_score": 0.31,
        "vp_altitude_score": 0.31,
        "global_affine_score": 0.31,
        "framing_breakout_score": 0.31,
    }
    result = detector.fuse(features)
    assert result.regime == UNCONSTRAINED
    assert result.confidence < detector.min_confidence
    assert "low_confidence" in result.reason_codes


def test_detector_handles_unsamplable_video(tmp_path):
    """``detect`` on a video file OpenCV can't read returns the ``unknown``
    fallback without raising — ingest must never crash on a malformed input."""
    bogus = tmp_path / "not-a-real.mp4"
    bogus.write_bytes(b"not a real video")
    detector = CaptureRegimeDetector()
    result = detector.detect(bogus)
    assert result.regime == UNKNOWN
    assert result.confidence == 0.0
    assert any(code in result.reason_codes for code in ("no_frames", "sample_failed"))


# ── Custom logistic coefficients override the fallback ───────────────────────


def test_custom_coefs_override_fallback(tmp_path, monkeypatch):
    """``REGIME_MODEL_PATH`` points to a joblib dict; coefs propagate to fuse()."""
    joblib = pytest.importorskip("joblib")
    coefs = {
        "intercept": 5.0,  # strong drone bias regardless of features
        "static_bg_score": 0.0,
        "vp_altitude_score": 0.0,
        "global_affine_score": 0.0,
        "framing_breakout_score": 0.0,
    }
    model_path = tmp_path / "regime_clf.joblib"
    joblib.dump(coefs, model_path)
    detector = CaptureRegimeDetector(model_path=str(model_path))
    result = detector.fuse(
        {
            "static_bg_score": 0.0,
            "vp_altitude_score": 0.0,
            "global_affine_score": 0.0,
            "framing_breakout_score": 0.0,
        }
    )
    assert result.regime == DRONE_FOLLOW


# ── The vanishing-point feature must actually be a feature ────────────────────


def _lines_frame(angle_deg: float, converge: bool, size: int = 400) -> np.ndarray:
    """A frame of straight white lines, either converging or parallel.

    Converging lines put the vanishing point near the frame (an elevated
    sideline look); parallel lines put it at infinity (a near-nadir look).
    """
    cv2 = pytest.importorskip("cv2")
    frame = np.zeros((size, size, 3), dtype=np.uint8)
    centre = size / 2.0
    rad = math.radians(angle_deg)
    for i in range(-3, 4):
        offset = i * size / 9.0
        if converge:
            # All lines aimed at one point just off the top of the frame.
            x0, y0 = centre + offset, float(size)
            x1, y1 = centre + offset * 0.15, -size * 0.5
        else:
            x0, y0 = centre + offset, float(size)
            x1, y1 = centre + offset, -float(size)

        def rot(x: float, y: float) -> tuple[int, int]:
            dx, dy = x - centre, y - centre
            return (
                int(centre + dx * math.cos(rad) - dy * math.sin(rad)),
                int(centre + dx * math.sin(rad) + dy * math.cos(rad)),
            )

        cv2.line(frame, rot(x0, y0), rot(x1, y1), (255, 255, 255), 2)
    return frame


class TestVanishingPointFeature:
    """It returned exactly 0.5 on all 30 real clips -- a constant, not a signal.

    The cause was a filter keeping only lines within 35 degrees of vertical,
    which assumes the yard lines recede away from the camera. That holds for a
    sideline camera and nothing else. Weighted at 1.1, the fallback added a
    fixed 0.55 to every logit: an intercept wearing a feature's name.
    """

    def test_parallel_lines_read_as_overhead(self) -> None:
        pytest.importorskip("cv2")
        assert rd._vanishing_point_score(_lines_frame(0.0, converge=False)) > 0.5

    def test_converging_lines_read_as_elevated_sideline(self) -> None:
        pytest.importorskip("cv2")
        assert rd._vanishing_point_score(_lines_frame(0.0, converge=True)) < 0.5

    @pytest.mark.parametrize("angle", [0.0, 30.0, 60.0, 90.0, 135.0])
    def test_the_reading_does_not_depend_on_orientation(self, angle: float) -> None:
        # The camera's roll is not evidence about its altitude. Rotating the
        # same geometry must not change the answer -- that equivalence is
        # exactly what the old near-vertical filter broke.
        pytest.importorskip("cv2")
        assert rd._vanishing_point_score(_lines_frame(angle, converge=False)) > 0.5

    def test_it_is_not_constant_across_inputs(self) -> None:
        # The regression that matters. Any single-value implementation passes
        # every threshold test above by luck; none passes this.
        pytest.importorskip("cv2")
        scores = {
            rd._vanishing_point_score(_lines_frame(a, converge=c))
            for a in (0.0, 45.0, 90.0)
            for c in (True, False)
        }
        assert len(scores) > 1

    def test_a_blank_frame_still_reports_no_evidence(self) -> None:
        pytest.importorskip("cv2")
        blank = np.zeros((200, 200, 3), dtype=np.uint8)
        assert rd._vanishing_point_score(blank) == pytest.approx(0.5)


class TestDominantFamily:
    """A converging family must survive grouping intact.

    Fixed-width clustering -- what the calibrator uses, and rightly -- shatters
    a fan: on the synthetic converging frame it split 20 lines spanning 177
    degrees into three clusters, so the spread got measured on one sliver. That
    underestimates convergence exactly on the low fixed-camera footage where it
    is largest, biasing the altitude score toward "drone" on the regime it most
    needs to recognise.
    """

    def test_a_fan_is_kept_whole(self) -> None:
        fan = [(100.0, math.radians(d)) for d in range(0, 60, 6)]
        assert len(rd._dominant_family(fan)) == len(fan)

    def test_genuinely_separate_directions_stay_separate(self) -> None:
        family = [(100.0, math.radians(d)) for d in (0, 4, 8)]
        other = [(200.0, math.radians(d)) for d in (85, 89)]
        assert len(rd._dominant_family(family + other)) == 3

    def test_a_family_straddling_the_wrap_point_is_one_family(self) -> None:
        # Angles are modulo pi, so 178 degrees and 2 degrees are 4 apart.
        straddling = [(100.0, math.radians(d)) for d in (172, 176, 179, 2, 6)]
        assert len(rd._dominant_family(straddling)) == 5

    def test_no_lines_is_not_an_error(self) -> None:
        assert rd._dominant_family([]) == []
