"""Pixel-only camera-motion compensation via chained ECC (Issue #138 §5.1).

Toledo film carries no SRT/GPS/IMU — only the MP4. In the DRONE_FOLLOW
regime the operator pans/zooms/tilts continuously, so a full homography
re-fit every frame is expensive and noisy. Instead we:

1. **Anchor** on every K-th frame (full calibration → ``H_k`` with
   confidence ``c_k``); keep anchors with ``c_k > ANCHOR_MIN_CONFIDENCE``.
2. **Inter-frame warp.** Between anchors, recover a 2D affine warp
   ``W_{t→t-1}`` with ``cv2.findTransformECC`` on the *non-player background
   mask* (grass + paint, players masked out).
3. **Chain.** ``H_t = W_{t→k} · H_k`` where ``W_{t→k}`` is the cumulative
   composition back to the most recent anchor.
4. **Consistency.** When the next anchor lands, compare the chained estimate
   to the direct fit; if re-projection drift exceeds ``REANCHOR_DRIFT_PX``,
   force a re-anchor.
5. **FIXED_SIDELINE shortcut.** ECC returns identity — the chain collapses to
   a single H for the whole game.

OpenCV is imported lazily; the chaining / zoom math is pure NumPy and
unit-tested directly.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

ANCHOR_INTERVAL = 15            # frames between full-calibration anchors (≈K)
ANCHOR_MIN_CONFIDENCE = 0.7     # only chain off confident anchors
REANCHOR_DRIFT_PX = 4.0         # chained-vs-direct drift that forces re-anchor
# Re-anchor when zoom magnitude exceeds 1.15× per second (Issue #138 AC).
MAX_ZOOM_PER_SECOND = 1.15


def affine_to_homography(warp: np.ndarray) -> np.ndarray:
    """Promote a 2×3 affine warp to a 3×3 homogeneous matrix."""
    H = np.eye(3, dtype=np.float64)
    H[:2, :] = np.asarray(warp, dtype=np.float64)
    return H


def zoom_magnitude(warp: np.ndarray) -> float:
    """Scale factor of an affine/homography warp from its linear part.

    Uses ``sqrt(|det(A)|)`` of the 2×2 linear block — 1.0 means no zoom.
    """
    a = np.asarray(warp, dtype=np.float64)[:2, :2]
    det = abs(float(np.linalg.det(a)))
    return math.sqrt(det) if det > 0 else 0.0


def exceeds_zoom_rate(
    warp: np.ndarray, *, frames_elapsed: int, fps: float
) -> bool:
    """True when the per-second zoom implied by ``warp`` exceeds the cap.

    ``warp`` is the cumulative warp over ``frames_elapsed`` frames. The
    per-second zoom is ``zoom ** (fps / frames_elapsed)``.
    """
    if frames_elapsed <= 0 or fps <= 0:
        return False
    z = zoom_magnitude(warp)
    if z <= 0:
        return False
    per_second = z ** (fps / float(frames_elapsed))
    return per_second > MAX_ZOOM_PER_SECOND or per_second < (1.0 / MAX_ZOOM_PER_SECOND)


def estimate_warp(
    prev_gray: np.ndarray,
    cur_gray: np.ndarray,
    *,
    mask: np.ndarray | None = None,
    motion_type: str = "affine",
    max_iters: int = 50,
    eps: float = 1e-4,
) -> tuple[np.ndarray, bool]:
    """Estimate the inter-frame affine warp between ``prev`` and ``cur`` via ECC.

    ``prev_gray`` is the ECC template, so the returned 2×3 warp maps the
    ``prev`` pixel grid onto ``cur`` (OpenCV ``findTransformECC`` convention).
    Returns ``(warp_2x3, success)``. On failure (ECC non-convergence or no
    cv2) returns the identity warp and ``False`` so callers can fall back to
    the last good estimate / force a re-anchor.
    """
    try:
        import cv2
    except Exception:
        return np.hstack([np.eye(2), np.zeros((2, 1))]), False

    mode = cv2.MOTION_AFFINE if motion_type == "affine" else cv2.MOTION_EUCLIDEAN
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        int(max_iters),
        float(eps),
    )
    p = prev_gray.astype(np.float32)
    c = cur_gray.astype(np.float32)
    try:
        _, warp = cv2.findTransformECC(
            p, c, warp, mode, criteria, mask, 5
        )
    except cv2.error:
        return np.hstack([np.eye(2), np.zeros((2, 1))]), False
    return warp.astype(np.float64), True


def background_mask(
    grass: np.ndarray, player_boxes: list[tuple[int, int, int, int]] | None
) -> np.ndarray:
    """Build the ECC alignment mask: grass region minus player bounding boxes.

    ``player_boxes`` are ``(x0, y0, x1, y1)`` in pixel coordinates (from the
    detector). The mask keeps static background so ECC aligns the *field*,
    not the moving players.
    """
    mask = (np.asarray(grass) > 0).astype(np.uint8) * 255
    if player_boxes:
        h, w = mask.shape[:2]
        for x0, y0, x1, y1 in player_boxes:
            x0 = max(0, min(w, int(x0)))
            x1 = max(0, min(w, int(x1)))
            y0 = max(0, min(h, int(y0)))
            y1 = max(0, min(h, int(y1)))
            mask[y0:y1, x0:x1] = 0
    return mask


class ChainedECCCompensator:
    """Maintains the most-recent anchor H and composes inter-frame warps onto it.

    Usage::

        comp = ChainedECCCompensator(regime="drone_follow", fps=60.0)
        comp.set_anchor(frame_idx, H_k, confidence)
        H_t, drift = comp.chain(frame_idx, warp_t_to_prev)
    """

    def __init__(self, *, regime: str = "drone_follow", fps: float = 60.0) -> None:
        self.regime = regime
        self.fps = float(fps)
        self.anchor_idx: int | None = None
        self.anchor_H: np.ndarray | None = None
        self.cumulative = np.eye(3, dtype=np.float64)  # W_{t→k}
        self.frames_since_anchor = 0

    @property
    def is_fixed_sideline(self) -> bool:
        return self.regime == "fixed_sideline"

    def set_anchor(self, frame_idx: int, H: np.ndarray, confidence: float) -> bool:
        """Register a new anchor if it clears the confidence floor."""
        if confidence < ANCHOR_MIN_CONFIDENCE:
            return False
        self.anchor_idx = frame_idx
        self.anchor_H = np.asarray(H, dtype=np.float64)
        self.cumulative = np.eye(3, dtype=np.float64)
        self.frames_since_anchor = 0
        return True

    def chain(self, warp_2x3: np.ndarray) -> tuple[np.ndarray | None, float]:
        """Compose one inter-frame warp onto the cumulative chain.

        FIXED_SIDELINE collapses to the anchor (identity warp). Returns the
        chained ``H_t`` (or ``None`` if no anchor yet) and the per-second zoom
        magnitude of the cumulative warp.
        """
        if self.is_fixed_sideline:
            # Camera bolted down: the chain is a single H for the whole clip.
            return self.anchor_H, 1.0
        if self.anchor_H is None:
            return None, 1.0
        W = affine_to_homography(warp_2x3)
        self.cumulative = W @ self.cumulative
        self.frames_since_anchor += 1
        H_t = self.cumulative @ self.anchor_H
        if abs(H_t[2, 2]) > 1e-12:
            H_t = H_t / H_t[2, 2]
        return H_t, zoom_magnitude(self.cumulative)

    def needs_reanchor(self, direct_H: np.ndarray, sample_pts: np.ndarray) -> bool:
        """Decide whether the chained estimate has drifted from a fresh fit.

        Compares the chained H to a directly-fit ``direct_H`` by re-projecting
        ``sample_pts`` through both; drift above ``REANCHOR_DRIFT_PX`` (or a
        zoom-rate breach) forces a re-anchor.
        """
        if self.anchor_H is None:
            return True
        if self.is_fixed_sideline:
            return False
        chained = self.cumulative @ self.anchor_H
        if abs(chained[2, 2]) > 1e-12:
            chained = chained / chained[2, 2]
        drift = _mean_reprojection_gap(chained, direct_H, sample_pts)
        if drift > REANCHOR_DRIFT_PX:
            return True
        return exceeds_zoom_rate(
            self.cumulative, frames_elapsed=self.frames_since_anchor, fps=self.fps
        )


def _mean_reprojection_gap(
    H_a: np.ndarray, H_b: np.ndarray, pts: np.ndarray
) -> float:
    """Mean pixel gap between two homographies over sample points."""
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) == 0:
        return 0.0
    homog = np.hstack([pts, np.ones((len(pts), 1))])
    pa = (H_a @ homog.T).T
    pb = (H_b @ homog.T).T
    pa = pa[:, :2] / np.where(np.abs(pa[:, 2:3]) < 1e-12, 1e-12, pa[:, 2:3])
    pb = pb[:, :2] / np.where(np.abs(pb[:, 2:3]) < 1e-12, 1e-12, pb[:, 2:3])
    return float(np.sqrt(((pa - pb) ** 2).sum(axis=1)).mean())


# ── Frame / background-mask helpers (cv2 lazy, no-op without it) ───────────────


def to_gray(frame: np.ndarray) -> np.ndarray:
    """Convert a BGR frame to single-channel grayscale for ECC.

    Falls back to a channel mean when OpenCV is unavailable so the pure-NumPy
    paths stay importable in test/CI environments without cv2.
    """
    arr = np.asarray(frame)
    if arr.ndim == 2:
        return arr.astype(np.uint8)
    try:
        import cv2

        return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    except Exception:
        return arr.mean(axis=2).astype(np.uint8)


def field_background_mask(
    frame_bgr: np.ndarray,
    player_boxes: list[tuple[int, int, int, int]] | None = None,
) -> np.ndarray:
    """Static-background mask for ECC: HSV grass + white paint, players removed.

    ECC must align the *field* — moving players would bias the warp — so we
    keep only grass (green hue band) and white paint (low saturation, high
    value) and zero out any supplied player bounding boxes. Without OpenCV we
    return an all-ones mask (ECC then aligns on the full frame).
    """
    arr = np.asarray(frame_bgr)
    try:
        import cv2
    except Exception:
        h, w = arr.shape[:2]
        full = np.full((h, w), 255, dtype=np.uint8)
        return background_mask(full, player_boxes)
    hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)
    grass = cv2.inRange(hsv, (30, 30, 30), (90, 255, 255))
    paint = cv2.inRange(hsv, (0, 0, 180), (180, 60, 255))
    field = cv2.bitwise_or(grass, paint)
    return background_mask(field, player_boxes)


# ── Sequence-level orchestration (chains warps onto sparsely-refit anchors) ────


@dataclass
class CompensationResult:
    """Outcome of chaining ECC warps across a consecutive-frame window.

    ``mean_drift_px`` is the §5.1 temporal-stability signal: the mean
    chained-vs-direct re-projection gap measured at each anchor checkpoint.
    """

    homographies: list[np.ndarray] = field(default_factory=list)
    mean_drift_px: float = 0.0
    max_drift_px: float = 0.0
    reanchor_count: int = 0
    zoom_breach: bool = False
    n_checks: int = 0
    n_frames: int = 0
    anchored: bool = False


def compensate_sequence(
    *,
    n_frames: int,
    regime: str,
    fps: float,
    anchor_fit: Callable[[int], tuple[np.ndarray, float] | None],
    warp_at: Callable[[int], tuple[np.ndarray, bool]],
    sample_pts: np.ndarray,
    anchor_interval: int = ANCHOR_INTERVAL,
) -> CompensationResult:
    """Chain inter-frame ECC warps onto sparsely-refit anchors (Issue #138).

    The pixel I/O is injected so this orchestration is unit-testable without
    OpenCV:

    * ``anchor_fit(i)`` returns a *direct* full-calibration ``(H_i, confidence)``
      for frame ``i`` (or ``None`` when the frame can't be calibrated). Called
      to seed the first anchor and again at each checkpoint.
    * ``warp_at(i)`` returns the inter-frame affine ``(warp_2x3, ok)`` mapping
      frame ``i-1`` onto frame ``i`` (typically :func:`estimate_warp`). ``ok``
      is ``False`` on ECC failure, forcing a re-anchor.

    Every ``anchor_interval`` frames the chained estimate is compared to a
    fresh direct fit; drift above ``REANCHOR_DRIFT_PX`` or a zoom-rate breach
    forces a re-anchor. FIXED_SIDELINE collapses to the single anchor (no-op).
    """
    sample_pts = np.asarray(sample_pts, dtype=np.float64)
    comp = ChainedECCCompensator(regime=regime, fps=fps)
    homographies: list[np.ndarray | None] = [None] * max(n_frames, 0)
    drifts: list[float] = []
    reanchor_count = 0
    zoom_breach = False

    # Seed the first anchor that clears the confidence floor.
    start: int | None = None
    for i in range(n_frames):
        fit = anchor_fit(i)
        if fit is not None and comp.set_anchor(i, fit[0], fit[1]):
            homographies[i] = comp.anchor_H
            start = i
            break
    if start is None:
        return CompensationResult(n_frames=n_frames, anchored=False)

    for i in range(start + 1, n_frames):
        warp, ok = warp_at(i)
        if not ok:
            # ECC could not converge → drop the chain and re-anchor here.
            fit = anchor_fit(i)
            if fit is not None and comp.set_anchor(i, fit[0], fit[1]):
                reanchor_count += 1
                homographies[i] = comp.anchor_H
            else:
                # No direct re-anchor available: reset the chain anchor to the
                # last known homography so later warps compose from this frame
                # forward. Otherwise the dropped inter-frame warp leaves the
                # cumulative motion missing one step and drifting thereafter.
                last_H = homographies[i - 1]
                homographies[i] = last_H
                if last_H is not None:
                    # Pass ANCHOR_MIN_CONFIDENCE so set_anchor clears its own
                    # floor: this is a forced continuity reset, not a fresh
                    # high-confidence calibration, so we only need it to take.
                    comp.set_anchor(i, last_H, ANCHOR_MIN_CONFIDENCE)
            continue

        H_t, _ = comp.chain(warp)
        homographies[i] = H_t if H_t is not None else homographies[i - 1]

        # Zoom-rate watchdog (AC #2): a fast zoom breaches 1.15×/s.
        if exceeds_zoom_rate(
            comp.cumulative, frames_elapsed=comp.frames_since_anchor, fps=fps
        ):
            zoom_breach = True

        # Anchor checkpoint every K frames: measure chained-vs-direct drift.
        if (
            comp.frames_since_anchor > 0
            and comp.frames_since_anchor % anchor_interval == 0
        ):
            fit = anchor_fit(i)
            if fit is None:
                continue
            direct_H, conf = fit
            chained = comp.cumulative @ comp.anchor_H
            if abs(chained[2, 2]) > 1e-12:
                chained = chained / chained[2, 2]
            drifts.append(_mean_reprojection_gap(chained, direct_H, sample_pts))
            if comp.needs_reanchor(direct_H, sample_pts):
                if comp.set_anchor(i, direct_H, conf):
                    reanchor_count += 1
                    homographies[i] = comp.anchor_H

    # Frames before the first anchor have no estimate; fill them with the first
    # anchor homography so the returned list stays aligned with the input
    # indices (length ``n_frames``).
    first_anchor_H = homographies[start]
    filled = [h if h is not None else first_anchor_H for h in homographies]
    return CompensationResult(
        homographies=filled,
        mean_drift_px=float(np.mean(drifts)) if drifts else 0.0,
        max_drift_px=float(np.max(drifts)) if drifts else 0.0,
        reanchor_count=reanchor_count,
        zoom_breach=zoom_breach,
        n_checks=len(drifts),
        n_frames=n_frames,
        anchored=True,
    )
