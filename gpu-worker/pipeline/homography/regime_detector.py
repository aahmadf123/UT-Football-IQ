"""Capture-regime detector (Issue #126).

Classifies a Toledo MP4 as one of:

* ``drone_follow``   — operator-flown drone with pans/zooms/tilts
* ``fixed_sideline`` — static elevated wide game camera
* ``unknown``        — safe fallback for low-confidence outputs

The detector is pixel-only (no SRT/GPS/IMU available from Hudl) and fuses
four cheap features with logistic regression:

1. **Static-background score.** Sample ``N_MEDIAN_FRAMES`` evenly spaced
   frames, compute the median image, and measure the mean absolute per-
   pixel deviation across the sampled stack. Low deviation ⇒ fixed
   camera; high deviation ⇒ drone pan/zoom.
2. **Vanishing-point altitude proxy.** Detect yard lines on a pre-snap
   frame with Hough; intersect near-vertical lines to estimate the
   vanishing-point height ``y_vp``. ``y_vp`` 2–6 image heights above the
   frame ⇒ elevated sideline; ``> 10`` ⇒ near-nadir drone.
3. **Optical-flow signature.** Average Farnebäck flow on the green-field
   region. A strong global affine component ⇒ drone-follow; a bimodal
   player-only motion distribution ⇒ fixed sideline.
4. **Aspect / framing prior.** Fraction of sampled frames where the
   green field reaches both left and right image edges (a coarse proxy
   for "both sidelines visible", which fixed-sideline cameras maintain
   and drone-follow cameras commonly do not).

Each feature is rescaled to ``[0, 1]`` where ``1`` favors
``drone_follow``. A logistic regression over the four features produces
``p(drone_follow)``; ``p ≥ 0.5 + margin`` → ``drone_follow``,
``p ≤ 0.5 - margin`` → ``fixed_sideline``, otherwise ``unknown``. Fusion
coefficients are loaded from a joblib artifact when present; otherwise
a hand-tuned linear fallback is used so unit tests and dev containers
work with no model file.

This module mirrors the adapter pattern used by ``segment_models`` and
``detector_models`` so a learned classifier can drop in later without
changing call sites.
"""

from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)


# ── Tunables (env-overridable) ────────────────────────────────────────────────

# Frames sampled to build the median / static-background statistic.
N_MEDIAN_FRAMES = 30
# Frames sampled for optical flow + framing prior. Kept small to bound cost.
N_FLOW_FRAMES = 8
# Image is downscaled before feature extraction (square long edge).
WORK_LONG_EDGE = 480

# Logistic threshold + uncertainty band. ``margin`` widens the dead zone
# that maps to ``unknown`` (see module docstring).
DEFAULT_MARGIN = 0.05

UNKNOWN = "unknown"
DRONE_FOLLOW = "drone_follow"
FIXED_SIDELINE = "fixed_sideline"
#: First-class "any camera" regime: the footage was analyzed but matches
#: neither special regime — endzone, handheld, tripod, phone, whatever. The
#: pipeline runs its full generic path (detection tuned for possibly-small
#: players, opportunistic calibration). ``unknown`` is reserved for hard
#: analysis failures (no frames / sampling error).
UNCONSTRAINED = "unconstrained"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass
class RegimeResult:
    regime: str
    confidence: float
    features: dict[str, float] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "regime": self.regime,
            "confidence": float(self.confidence),
            "features": dict(self.features),
            "reason_codes": list(self.reason_codes),
        }


# ── Adapter base ──────────────────────────────────────────────────────────────


class RegimeDetectorAdapter(ABC):
    """Abstract base for capture-regime detectors.

    Subclasses implement ``detect(video_path)`` and return a
    :class:`RegimeResult`. The adapter shape mirrors
    ``SegmenterBase``/``DetectorBase`` so the pipeline can swap in a
    learned classifier later without touching call sites.
    """

    source: str = "abstract"

    @abstractmethod
    def detect(self, video_path: Path) -> RegimeResult: ...


# ── Default heuristic + logistic-fusion implementation ────────────────────────


# Hand-tuned linear-fusion fallback. Each feature is rescaled to ``[0,1]``
# where ``1`` favors ``drone_follow`` (see ``_extract_features``). Bias is
# centered near 0 so the four features collectively swing the logit.
_FALLBACK_COEFS: dict[str, float] = {
    "intercept": -2.5,
    "static_bg_score": 1.6,
    "vp_altitude_score": 1.1,
    "global_affine_score": 1.4,
    "framing_breakout_score": 0.9,
}


def _logistic(z: float) -> float:
    if z >= 0:
        e = math.exp(-z)
        return 1.0 / (1.0 + e)
    e = math.exp(z)
    return e / (1.0 + e)


def _load_coefs(model_path: str | None) -> dict[str, float]:
    """Load logistic-regression coefficients from a joblib artifact.

    Returns the hand-tuned fallback when the path is missing, unreadable,
    or doesn't match the expected schema. ``joblib`` is imported lazily
    so the module stays importable in test containers that don't ship
    sklearn/joblib.
    """
    if not model_path:
        return _FALLBACK_COEFS
    p = Path(model_path)
    if not p.exists():
        return _FALLBACK_COEFS
    try:
        import joblib  # type: ignore[import-not-found]

        payload = joblib.load(p)
    except Exception as exc:
        log.warning("regime_model_load_failed", path=str(p), error=str(exc))
        return _FALLBACK_COEFS

    if isinstance(payload, dict) and "intercept" in payload:
        # Minimal contract: a dict with an intercept and one entry per
        # feature name. Any missing feature falls back to the hand-tuned
        # value so partial artifacts don't silently zero out a signal.
        merged = dict(_FALLBACK_COEFS)
        for k, v in payload.items():
            try:
                merged[k] = float(v)
            except (TypeError, ValueError):
                continue
        return merged
    return _FALLBACK_COEFS


class CaptureRegimeDetector(RegimeDetectorAdapter):
    """Heuristic + logistic-regression capture-regime detector."""

    source = "heuristic_v1"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        margin: float | None = None,
    ) -> None:
        self.margin = (
            margin
            if margin is not None
            else _env_float("REGIME_MARGIN", DEFAULT_MARGIN)
        )
        self.min_confidence = _env_float("REGIME_MIN_CONFIDENCE", 0.55)
        self.coefs = _load_coefs(model_path or os.environ.get("REGIME_MODEL_PATH", ""))

    # ── Public API ────────────────────────────────────────────────────────

    def detect(self, video_path: Path) -> RegimeResult:
        try:
            frames = _sample_frames(video_path, N_MEDIAN_FRAMES)
        except Exception as exc:
            log.warning("regime_sample_failed", error=str(exc))
            return RegimeResult(
                regime=UNKNOWN, confidence=0.0, reason_codes=["sample_failed"]
            )
        if not frames:
            return RegimeResult(
                regime=UNKNOWN, confidence=0.0, reason_codes=["no_frames"]
            )
        features = self.extract_features(frames)
        return self.fuse(features)

    # ── Feature extraction ────────────────────────────────────────────────

    def extract_features(self, frames: list[np.ndarray]) -> dict[str, float]:
        """Compute the four pixel-only features on a list of BGR frames."""
        return _extract_features(frames)

    # ── Fusion ────────────────────────────────────────────────────────────

    def fuse(self, features: dict[str, float]) -> RegimeResult:
        """Apply logistic regression to features and threshold to a regime."""
        z = float(self.coefs.get("intercept", 0.0))
        for key, weight in self.coefs.items():
            if key == "intercept":
                continue
            z += float(features.get(key, 0.0)) * float(weight)
        p_drone = _logistic(z)
        confidence = abs(p_drone - 0.5) * 2.0  # 0..1, peaks at the extremes
        reason_codes: list[str] = []

        # A confident non-match is not a failure: footage that fits neither
        # special regime is first-class "unconstrained" capture (any angle,
        # any height, any camera) and takes the generic pipeline path.
        if 0.5 - self.margin < p_drone < 0.5 + self.margin:
            regime = UNCONSTRAINED
            reason_codes.append("within_margin")
        elif confidence < self.min_confidence:
            regime = UNCONSTRAINED
            reason_codes.append("low_confidence")
        elif p_drone >= 0.5 + self.margin:
            regime = DRONE_FOLLOW
        elif p_drone <= 0.5 - self.margin:
            regime = FIXED_SIDELINE
        else:
            regime = UNCONSTRAINED

        return RegimeResult(
            regime=regime,
            confidence=round(float(confidence), 4),
            features={k: round(float(v), 4) for k, v in features.items()},
            reason_codes=reason_codes,
        )


# ── Feature implementations (module-level so tests can hit them directly) ─────


def _extract_features(frames: list[np.ndarray]) -> dict[str, float]:
    if not frames:
        return {
            "static_bg_score": 0.0,
            "vp_altitude_score": 0.0,
            "global_affine_score": 0.0,
            "framing_breakout_score": 0.0,
        }

    stack = np.stack([_to_work_size(f) for f in frames], axis=0)
    return {
        "static_bg_score": _static_background_score(stack),
        "vp_altitude_score": _vanishing_point_score(stack[0]),
        "global_affine_score": _global_affine_score(stack[: max(2, N_FLOW_FRAMES)]),
        "framing_breakout_score": _framing_breakout_score(stack),
    }


def _to_work_size(frame: np.ndarray) -> np.ndarray:
    """Downscale a frame so its long edge is ``WORK_LONG_EDGE``."""
    h, w = frame.shape[:2]
    long_edge = max(h, w)
    if long_edge <= WORK_LONG_EDGE:
        return frame.astype(np.uint8, copy=False)
    scale = WORK_LONG_EDGE / long_edge
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    try:
        import cv2  # local import keeps this module importable without cv2

        return cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    except Exception:
        # Pure-numpy fallback: nearest-neighbor stride. Good enough for tests.
        ys = (np.linspace(0, h - 1, new_h)).astype(np.int32)
        xs = (np.linspace(0, w - 1, new_w)).astype(np.int32)
        return frame[ys][:, xs]


def _static_background_score(stack: np.ndarray) -> float:
    """High when the per-pixel deviation across the sampled stack is large
    (drone pan), low when frames sit on a near-constant median (fixed
    camera). Normalised by 255 and clipped to ``[0, 1]``.
    """
    if stack.shape[0] < 2:
        return 0.0
    f32 = stack.astype(np.float32)
    median = np.median(f32, axis=0)
    deviation = np.mean(np.abs(f32 - median))
    # 0..255 deviation; 12 is roughly the boundary observed on practice
    # vs game footage in §3 of the research report. Clip to [0,1].
    return float(min(1.0, max(0.0, deviation / 12.0)))


def _vanishing_point_score(frame: np.ndarray) -> float:
    """Estimate vertical position of the vanishing point of near-vertical
    lines and translate to a 0..1 score where ``1`` favors drone-follow
    (very-high VP / near-nadir view) and ``0`` favors elevated sideline.

    Falls back to ``0.5`` when not enough lines are found.
    """
    try:
        import cv2
    except Exception:
        return 0.5
    if frame.ndim == 3:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    else:
        gray = frame
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=120)
    if lines is None or len(lines) < 2:
        return 0.5

    h, w = gray.shape[:2]
    # Keep near-vertical lines only (yard lines projected toward VP).
    vert_lines: list[tuple[float, float]] = []
    for line in lines[:50]:
        rho, theta = line[0]
        # Vertical-ish in image space: theta near 0 or pi.
        if min(abs(theta), abs(theta - math.pi)) > math.radians(35):
            continue
        vert_lines.append((float(rho), float(theta)))
    if len(vert_lines) < 2:
        return 0.5

    intersections_y: list[float] = []
    for i in range(len(vert_lines)):
        r1, t1 = vert_lines[i]
        for j in range(i + 1, len(vert_lines)):
            r2, t2 = vert_lines[j]
            # Solve [cos(t1) sin(t1); cos(t2) sin(t2)] · [x; y] = [r1; r2]
            # for y via Cramer's rule. Determinant: sin(t2 - t1).
            denom = math.sin(t2 - t1)
            if abs(denom) < 1e-3:
                continue
            y = (r2 * math.cos(t1) - r1 * math.cos(t2)) / denom
            intersections_y.append(y)
    if not intersections_y:
        return 0.5

    y_vp = float(np.median(intersections_y))
    # ``y_vp`` is measured in pixels from the top of the image; negative
    # values mean the VP is above the frame. Convert to "image heights
    # above the top of the frame": positive == above.
    heights_above = max(0.0, -y_vp) / max(1.0, float(h))

    # Map heights_above → score. <2 image heights or below frame → fixed
    # (score 0); >10 → drone (score 1); linear in between.
    if heights_above <= 2.0:
        return 0.0
    if heights_above >= 10.0:
        return 1.0
    return float((heights_above - 2.0) / 8.0)


def _global_affine_score(stack: np.ndarray) -> float:
    """Higher when consecutive frames show a strong global affine motion
    (camera pan/zoom/tilt); lower when motion is localised to small
    regions (players on a static background).

    Pure-numpy frame-difference proxy: compare consecutive frames after
    masking the top half of the image (which is usually sky/stands).
    Drone pans shift the field horizontally → large mean-magnitude diff
    in the bottom half. Player-only motion → small mean-magnitude diff
    relative to the spatial variance of the diff (it's localised).
    """
    if stack.shape[0] < 2:
        return 0.0
    f32 = stack.astype(np.float32)
    h = f32.shape[1]
    bottom = f32[:, h // 2 :, :, :]
    diffs = np.abs(np.diff(bottom, axis=0))  # (T-1, H/2, W, C)
    mean_per_frame = diffs.mean(axis=(1, 2, 3))  # (T-1,)
    std_per_frame = diffs.std(axis=(1, 2, 3)) + 1e-6
    mean_mag = float(mean_per_frame.mean())
    # Mean / std ratio: high when motion is uniform (camera shift), low
    # when motion is concentrated in a few pixels (players).
    uniformity = float(np.mean(mean_per_frame / std_per_frame))
    # Combine: a moderate-magnitude, uniform diff means drone. Threshold
    # numbers come from §3 reference report.
    magnitude_score = min(1.0, max(0.0, mean_mag / 8.0))
    uniformity_score = min(1.0, max(0.0, (uniformity - 0.4) / 0.6))
    return float(0.6 * magnitude_score + 0.4 * uniformity_score)


def _framing_breakout_score(stack: np.ndarray) -> float:
    """High when the green field does NOT consistently reach both image
    edges (a drone-follow signature — far sideline is often cropped).
    Low when the field reaches both edges (fixed wide camera).

    Per-frame: classify a pixel as field when the green channel dominates
    and luminance is moderate (a cheap stand-in for HSV thresholding that
    avoids the cv2 dependency). Then check whether at least one column in
    the leftmost ``edge_width`` and one in the rightmost ``edge_width``
    is mostly field. The reported score is ``1 - mean(both_edges_in)``.
    """
    if stack.size == 0:
        return 0.0
    breakouts: list[float] = []
    for frame in stack:
        f = frame.astype(np.float32)
        b, g, r = f[..., 0], f[..., 1], f[..., 2]
        # OpenCV uses BGR order. Field-ish: green dominant, mid-luminance.
        field_mask = (g > b + 10) & (g > r + 10) & (g > 50) & (g < 220)
        if not np.any(field_mask):
            breakouts.append(1.0)
            continue
        h, w = field_mask.shape
        edge_width = max(2, w // 20)
        left_cols = field_mask[:, :edge_width]
        right_cols = field_mask[:, -edge_width:]
        # A side counts as "field-touching" if ≥30% of its band is field.
        left_in = float(left_cols.mean() > 0.30)
        right_in = float(right_cols.mean() > 0.30)
        breakouts.append(1.0 - 0.5 * (left_in + right_in))
    return float(np.mean(breakouts))


# ── Frame sampling ────────────────────────────────────────────────────────────


def _sample_frames(video_path: Path, n: int) -> list[np.ndarray]:
    """Sample up to ``n`` evenly spaced BGR frames from a video.

    Returns an empty list when OpenCV can't open the file or extract any
    frames. Callers treat that as the ``unknown`` fallback path.
    """
    try:
        import cv2
    except Exception:
        return []
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        duration = total / fps if fps > 0 else 0.0
        if total <= 0 or duration <= 0:
            return []
        times = np.linspace(
            0.05 * duration, 0.95 * duration, num=max(1, n), endpoint=True
        )
        frames: list[np.ndarray] = []
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
        return frames
    finally:
        cap.release()
