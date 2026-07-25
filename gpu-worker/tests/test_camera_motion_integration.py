"""Sequence-level chained-ECC integration tests (Issue #138 §5.1).

These exercise :func:`compensate_sequence` — the orchestration that
``stage_calibrate`` now calls for the DRONE_FOLLOW regime — against the
issue's acceptance criteria:

* Mean re-projection drift < 2 px over a ~5 s DRONE_FOLLOW window.
* A zoom faster than 1.15×/s forces a re-anchor.
* FIXED_SIDELINE collapses to a single anchor (a no-op).

The pixel I/O is injected, so the core criteria are validated in pure NumPy
without OpenCV. A final cv2-gated test runs the *real* ECC estimator
end-to-end on synthetic frames.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.homography.camera_motion_ecc import (
    ANCHOR_INTERVAL,
    compensate_sequence,
    estimate_warp,
)

CORNERS = np.array([[0, 0], [1280, 0], [1280, 720], [0, 720]], dtype=np.float64)


def _translation(tx: float, ty: float) -> np.ndarray:
    return np.array([[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]])


def _scale(s: float) -> np.ndarray:
    return np.array([[s, 0.0, 0.0], [0.0, s, 0.0], [0.0, 0.0, 1.0]])


# ── AC #1: mean drift < 2 px over a 5 s drone window ──────────────────────────


def test_drone_window_drift_under_two_px():
    rng = np.random.default_rng(0)
    fps = 15.0
    n_frames = int(5 * fps) + 1  # ~5 second window
    per_step = (3.0, -1.5)       # steady operator pan, px/frame

    def anchor_fit(i):
        # Ground-truth direct calibration at frame i (cumulative pan).
        return _translation(per_step[0] * i, per_step[1] * i), 0.9

    def warp_at(i):
        # ECC recovers the inter-frame pan with small sub-pixel noise.
        noise = rng.normal(0.0, 0.03, size=2)
        warp = np.array(
            [[1.0, 0.0, per_step[0] + noise[0]],
             [0.0, 1.0, per_step[1] + noise[1]]]
        )
        return warp, True

    result = compensate_sequence(
        n_frames=n_frames, regime="drone_follow", fps=fps,
        anchor_fit=anchor_fit, warp_at=warp_at, sample_pts=CORNERS,
    )

    assert result.anchored
    assert result.n_checks >= 1
    # Chained estimate tracks the direct fit to well under the 2 px target.
    assert result.mean_drift_px < 2.0
    assert result.max_drift_px < 2.0
    assert not result.zoom_breach
    assert len(result.homographies) == n_frames


def test_ecc_failure_forces_reanchor():
    # ECC never converges → every step re-anchors off a fresh direct fit.
    def anchor_fit(i):
        return _translation(2.0 * i, 0.0), 0.9

    def warp_at(i):
        return np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]), False

    result = compensate_sequence(
        n_frames=10, regime="drone_follow", fps=15.0,
        anchor_fit=anchor_fit, warp_at=warp_at, sample_pts=CORNERS,
    )
    assert result.anchored
    assert result.reanchor_count == 9  # frames 1..9 each re-anchor


# ── AC #2: zoom faster than 1.15×/s forces a re-anchor ────────────────────────


def test_fast_zoom_triggers_reanchor():
    fps = 15.0
    n_frames = ANCHOR_INTERVAL + 2

    def anchor_fit(i):
        # Direct fit matches the (zoomed) chain, so drift stays ~0; the
        # re-anchor must be driven purely by the zoom-rate watchdog.
        return _scale(1.05**i), 0.9

    def warp_at(i):
        return np.array([[1.05, 0.0, 0.0], [0.0, 1.05, 0.0]]), True

    result = compensate_sequence(
        n_frames=n_frames, regime="drone_follow", fps=fps,
        anchor_fit=anchor_fit, warp_at=warp_at, sample_pts=CORNERS,
    )
    # 1.05/frame ⇒ ~1.05**15 ≈ 2.1×/s, far above the 1.15×/s cap.
    assert result.zoom_breach
    assert result.reanchor_count >= 1


# ── AC #3: FIXED_SIDELINE path is a no-op (single anchor H) ───────────────────


def test_fixed_sideline_window_is_noop():
    anchor = _translation(10.0, 5.0)

    def anchor_fit(i):
        return anchor, 0.9

    def warp_at(i):
        # Even with large bogus motion, the fixed chain ignores warps.
        return np.array([[1.3, 0.0, 99.0], [0.0, 1.3, 99.0]]), True

    result = compensate_sequence(
        n_frames=40, regime="fixed_sideline", fps=30.0,
        anchor_fit=anchor_fit, warp_at=warp_at, sample_pts=CORNERS,
    )
    assert result.anchored
    assert result.n_checks == 0          # no checkpoints: chain never advances
    assert result.reanchor_count == 0
    assert not result.zoom_breach
    assert result.mean_drift_px == 0.0
    # Every frame collapses to the single anchor homography.
    for H in result.homographies:
        assert np.allclose(H, anchor)


def test_no_anchorable_frame_returns_unanchored():
    result = compensate_sequence(
        n_frames=20, regime="drone_follow", fps=15.0,
        anchor_fit=lambda i: None,                 # nothing calibrates
        warp_at=lambda i: (np.eye(2, 3), True),
        sample_pts=CORNERS,
    )
    assert not result.anchored
    assert result.n_checks == 0


# ── Real ECC end-to-end on synthetic frames (cv2 required) ────────────────────


def test_real_ecc_window_drift_under_two_px():
    cv2 = pytest.importorskip("cv2", reason="real ECC needs OpenCV")

    h, w = 200, 260
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.clip(
        128 + 60 * np.sin(2 * np.pi * xx / 40.0) + 50 * np.cos(2 * np.pi * yy / 35.0),
        0, 255,
    ).astype(np.uint8)

    per_step = (2.0, 1.0)  # known pan, px/frame
    n_frames = ANCHOR_INTERVAL + 1
    frames = [
        cv2.warpAffine(
            base,
            np.array([[1.0, 0.0, per_step[0] * i], [0.0, 1.0, per_step[1] * i]],
                     dtype=np.float32),
            (w, h),
            borderMode=cv2.BORDER_REFLECT101,
        )
        for i in range(n_frames)
    ]
    masks = [np.full((h, w), 255, dtype=np.uint8) for _ in frames]
    pts = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)

    def anchor_fit(i):
        return _translation(per_step[0] * i, per_step[1] * i), 0.9

    def warp_at(i):
        return estimate_warp(frames[i - 1], frames[i], mask=masks[i - 1], max_iters=200)

    result = compensate_sequence(
        n_frames=n_frames, regime="drone_follow", fps=15.0,
        anchor_fit=anchor_fit, warp_at=warp_at, sample_pts=pts,
    )
    assert result.anchored
    assert result.n_checks >= 1
    assert result.mean_drift_px < 2.0
