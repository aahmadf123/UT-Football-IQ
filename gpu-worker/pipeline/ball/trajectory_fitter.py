"""Ball trajectory fit — single camera, no altitude (Issue #134).

Without a second camera or telemetry we cannot recover true 3-D ball height,
but the coach-facing metrics do not need it. Two usable signals (research
report §3.6.2 / §7.11):

1. **Field-plane projection** (z = 0 at snap and catch). Project the ball's
   image position at the snap and catch frames through the calibration
   homography ``H`` into field yards; the throw distance is the Euclidean
   distance on that plane.
2. **Image-y parabola.** The ball's image-y traces a near-parabola in time:
   ``y_img(t) = y0 + v_y·t − ½·α·t²`` where ``α`` absorbs the unknown
   pixel-per-foot × g product. Hang time = catch − snap; apex = argmin y_img.

For the ``drone_follow`` regime the global affine flow (already computed by the
ECC camera-motion stage for BoT-SORT) is subtracted before fitting so the
parabola reflects ball motion, not camera pan.

A **physics regulariser** rejects fits outside the realistic football throw
envelope (18–32 ft/s release, 30–45° launch).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import structlog

from pipeline.homography.project import apply_homography as _apply_homography

log = structlog.get_logger(__name__)

# Realistic football throw envelope (research report §7.11).
MIN_RELEASE_FPS: float = 18.0
MAX_RELEASE_FPS: float = 32.0
MIN_LAUNCH_DEG: float = 30.0
MAX_LAUNCH_DEG: float = 45.0


@dataclass(frozen=True)
class ParabolaFit:
    """Least-squares fit of ``y_img(t) = y0 + v_y·t − ½·α·t²``."""

    y0: float
    v_y: float
    alpha: float
    rms_residual: float
    apex_t: float
    apex_y: float

    @property
    def well_formed(self) -> bool:
        """True when the fit is genuinely curved (a real arc, not a line).

        Image-y is down-positive, so a thrown ball traces an upward-opening
        curve (minimum at apex) and ``alpha`` is negative; a held/rolling ball
        gives ``alpha ≈ 0``. Curvature — not its sign — is what marks an arc.
        """
        return abs(self.alpha) > 1e-6


def fit_parabola(times: list[float], y_img: list[float]) -> ParabolaFit | None:
    """Fit a quadratic to the image-y curve. ``None`` if under-determined."""
    if len(times) < 3 or len(times) != len(y_img):
        return None
    t = np.asarray(times, dtype=np.float64)
    y = np.asarray(y_img, dtype=np.float64)
    # y = a·t² + b·t + c   ⇒   α = −2a, v_y = b, y0 = c
    a, b, c = np.polyfit(t, y, 2)
    alpha = -2.0 * float(a)
    pred = a * t * t + b * t + c
    rms = float(np.sqrt(np.mean((y - pred) ** 2)))
    apex_t = float(-b / (2.0 * a)) if abs(a) > 1e-12 else float(t[len(t) // 2])
    apex_y = float(a * apex_t * apex_t + b * apex_t + c)
    return ParabolaFit(float(c), float(b), alpha, rms, apex_t, apex_y)


# Re-exported, not re-implemented. Projection now lives in
# ``pipeline.homography.project`` so the track stage can use it without
# importing ``pipeline.ball``; this alias keeps every existing ball call site
# working and guarantees there is only one implementation to get wrong.
apply_homography = _apply_homography


def field_plane_distance(
    H: np.ndarray,
    snap_point: tuple[float, float],
    catch_point: tuple[float, float],
) -> float:
    """Throw distance in field yards between two image points via ``H``."""
    sx, sy = apply_homography(H, snap_point)
    cx, cy = apply_homography(H, catch_point)
    return float(np.hypot(cx - sx, cy - sy))


def compensate_affine(
    points: dict[int, tuple[float, float]],
    affine_by_frame: dict[int, np.ndarray],
) -> dict[int, tuple[float, float]]:
    """Map per-frame ball points into a stabilised reference using ECC affines.

    ``affine_by_frame[f]`` is the 2×3 affine that maps frame ``f`` pixel
    coordinates into the reference frame (identity for the reference). Frames
    without an affine are passed through unchanged.
    """
    out: dict[int, tuple[float, float]] = {}
    for f, (x, y) in points.items():
        A = affine_by_frame.get(f)
        if A is None:
            out[f] = (x, y)
            continue
        v = np.array([x, y, 1.0], dtype=np.float64)
        p = np.asarray(A, dtype=np.float64) @ v
        out[f] = (float(p[0]), float(p[1]))
    return out


def is_realistic_throw(release_speed_fps: float, launch_angle_deg: float) -> bool:
    """Physics regulariser: accept only fits inside the throw envelope."""
    return (
        MIN_RELEASE_FPS <= release_speed_fps <= MAX_RELEASE_FPS
        and MIN_LAUNCH_DEG <= launch_angle_deg <= MAX_LAUNCH_DEG
    )


def hang_time_seconds(snap_frame: int, catch_frame: int, fps: float) -> float:
    """Hang time in seconds between snap/release and catch."""
    if fps <= 0:
        return 0.0
    return max(0.0, (catch_frame - snap_frame) / fps)
