"""Unit tests for the capture-regime detector (Issue #126).

Covers all four pixel-only features and the logistic-fusion path on five
synthetic "clips" built in memory. The tests deliberately avoid the
OpenCV-dependent ``_sample_frames`` helper — they feed the four features
straight into ``CaptureRegimeDetector.fuse(...)`` or call the feature
functions on hand-crafted numpy stacks. That keeps the suite
deterministic and runnable in any container that has numpy.
"""

from __future__ import annotations

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
