"""Referee / official suppression (Issue #148, research report §5.12).

Officials (referees, umpires, line judges) corrupt team classification,
formation analysis, and personnel counts if they stay in the player pool.
NCAA officials wear high-contrast vertical black-and-white striped shirts,
which is a strong, cheap visual signature.

This module relabels — it never deletes. A detection flagged as an official
keeps its bbox/confidence and is reclassified ``class = "official"`` so it is
still *detected separately* (Issue #128 acceptance criterion) and downstream
stages (``stage_labels``) can filter it out before team/formation analysis.
Leaving the box in the detection set also means a false positive only costs a
relabel, never a deleted player, which is the conservative failure mode the
acceptance criteria require ("without deleting valid players").

Two signals, combined conservatively:

1. **Vertical-stripe periodicity** — pure-NumPy power-spectrum analysis of the
   torso column-luminance profile. Authoritative, deterministic, and runnable
   in any container with NumPy (no OpenCV dependency for the decision).
2. **Black/white bimodality** — fraction of torso pixels that are near-black or
   near-white, the referee-shirt palette.

An optional Gabor filter bank (the §5.12 reference method) refines the score
when OpenCV is available, but is never required for the decision so behaviour
is identical in CI.

An optional ``field_of_play`` polygon lets the caller additionally tag
detections whose foot point falls outside the field as ``class = "sideline"``
(chain crew / sideline overflow). This is off unless a polygon is supplied.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# ── Tunables (env-overridable) ────────────────────────────────────────────────

# Combined-score threshold above which a detection is relabeled ``official``.
# Conservative by default to keep the player→official false-positive rate low
# (Issue #148 target: < 2%).
OFFICIAL_SCORE_THRESHOLD = float(os.environ.get("OFFICIAL_SCORE_THRESHOLD", "0.6"))
# Master switch. When false, ``suppress`` is a no-op pass-through.
SUPPRESSION_ENABLED = os.environ.get("OFFICIAL_SUPPRESSION_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

OFFICIAL_CLASS = "official"
SIDELINE_CLASS = "sideline"
PLAYER_CLASS = "player"
# Classes excluded from team/formation analysis downstream.
NON_PLAYER_CLASSES = frozenset({OFFICIAL_CLASS, SIDELINE_CLASS})

# Torso band of the bbox used for the jersey crop (skip head + legs).
_TORSO_TOP = 0.15
_TORSO_BOTTOM = 0.55
# Stripe-count band searched in the column power spectrum (a referee shirt
# shows a handful of vertical stripes across the torso width).
_MIN_CYCLES = 2
_MAX_CYCLES = 14


def _to_gray(crop: np.ndarray) -> np.ndarray:
    if crop.ndim == 2:
        return crop.astype(np.float32)
    # BGR luminance (OpenCV order); coefficients are the usual Rec.601 weights.
    b, g, r = crop[..., 0], crop[..., 1], crop[..., 2]
    return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.float32)


def vertical_stripe_score(crop: np.ndarray) -> float:
    """Fraction of column-profile energy concentrated in a few vertical
    stripes, scaled by stripe contrast. ``~1`` for a clean B/W stripe pattern,
    ``~0`` for a flat / uniform crop. Pure NumPy — no OpenCV required.
    """
    if crop.size == 0:
        return 0.0
    gray = _to_gray(crop)
    if gray.shape[0] < 2 or gray.shape[1] < 4:
        return 0.0
    # Column luminance profile across the width.
    profile = gray.mean(axis=0)
    profile = profile - profile.mean()
    contrast = float(np.std(profile)) / 64.0  # 64 LSB std ≈ strong stripes
    contrast = min(1.0, contrast)
    if contrast <= 0.0:
        return 0.0
    power = np.abs(np.fft.rfft(profile)) ** 2
    if power.shape[0] <= 1:
        return 0.0
    ac = power[1:]  # drop DC
    total = float(ac.sum())
    if total <= 0.0:
        return 0.0
    hi = min(_MAX_CYCLES, ac.shape[0])
    band = ac[_MIN_CYCLES - 1 : hi]
    if band.size == 0:
        return 0.0
    peak_ratio = float(band.max() / total)
    return float(min(1.0, peak_ratio * contrast * 2.0))


def bw_bimodality_score(crop: np.ndarray) -> float:
    """Fraction of pixels that are near-black or near-white — the referee
    black/white shirt palette. ``~1`` for a B/W crop, lower for jersey colors.
    """
    if crop.size == 0:
        return 0.0
    gray = _to_gray(crop)
    near_black = gray < 60.0
    near_white = gray > 195.0
    extreme = float(np.mean(near_black | near_white))
    return float(min(1.0, extreme))


def gabor_stripe_response(crop: np.ndarray) -> float | None:
    """Optional OpenCV Gabor-bank refinement (§5.12). Returns ``None`` when
    OpenCV is unavailable so the caller can fall back to the NumPy signals.
    """
    try:
        import cv2
    except Exception:
        return None
    gray = _to_gray(crop).astype(np.float32)
    if gray.shape[0] < 4 or gray.shape[1] < 4:
        return None
    gray /= 255.0
    responses = []
    # Vertical stripes → kernels oriented to respond to vertical edges
    # (theta ≈ 0). Sweep a few stripe wavelengths (8–12 px, §5.12). The kernel
    # is zero-meaned so a flat (uniform) crop yields a near-zero response, and
    # we use the response *std* so the metric measures texture, not brightness.
    for wavelength in (8.0, 10.0, 12.0):
        kernel = cv2.getGaborKernel(
            (15, 15), sigma=4.0, theta=0.0, lambd=wavelength, gamma=0.5, psi=0.0
        )
        kernel = kernel - float(kernel.mean())
        filtered = cv2.filter2D(gray, cv2.CV_32F, kernel)
        responses.append(float(np.std(filtered)))
    if not responses:
        return None
    return float(min(1.0, max(responses) * 4.0))


def official_score(crop: np.ndarray) -> float:
    """Combined official-likelihood in ``[0, 1]`` for a jersey crop."""
    stripe = vertical_stripe_score(crop)
    bw = bw_bimodality_score(crop)
    base = 0.6 * stripe + 0.4 * bw
    gabor = gabor_stripe_response(crop)
    if gabor is not None:
        # Average the NumPy base with the Gabor refinement; both must agree
        # for a strong score, keeping false positives low.
        base = 0.5 * base + 0.5 * (0.6 * gabor + 0.4 * bw)
    return float(min(1.0, base))


def _torso_crop(frame: np.ndarray, bbox: list[float]) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    bh = y2 - y1
    ty1 = int(round(y1 + _TORSO_TOP * bh))
    ty2 = int(round(y1 + _TORSO_BOTTOM * bh))
    cx1 = max(0, int(round(x1)))
    cx2 = min(w, int(round(x2)))
    cy1 = max(0, min(h, ty1))
    cy2 = max(0, min(h, ty2))
    if cx2 <= cx1 or cy2 <= cy1:
        return np.empty((0, 0, 3), dtype=frame.dtype)
    return frame[cy1:cy2, cx1:cx2]


def _foot_point(bbox: list[float]) -> tuple[float, float]:
    x1, _y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, y2)


def _point_in_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test (pure Python)."""
    inside = False
    n = len(polygon)
    if n < 3:
        return True  # no usable polygon → treat everything as in-field
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


class OfficialSuppressor:
    """Relabels striped officials (and optionally off-field sideline figures).

    Args:
        threshold: combined-score cut-off for the ``official`` label.
        enabled:   master switch (defaults to the module/env setting).
        field_of_play: optional pixel-space polygon ``[[x, y], ...]`` of the
            in-bounds field. Player-class detections whose foot point falls
            outside it are relabeled ``sideline``.
    """

    def __init__(
        self,
        *,
        threshold: float | None = None,
        enabled: bool | None = None,
        field_of_play: list[list[float]] | None = None,
    ) -> None:
        self.threshold = OFFICIAL_SCORE_THRESHOLD if threshold is None else threshold
        self.enabled = SUPPRESSION_ENABLED if enabled is None else enabled
        self.field_of_play = field_of_play

    def classify(self, frame: np.ndarray, det: dict[str, Any]) -> str:
        """Return the (possibly relabeled) class for a single detection.

        Only ``player`` detections are ever reclassified — a ``ball`` is never
        touched. Officials take precedence over the sideline tag.
        """
        cls = det.get("class", PLAYER_CLASS)
        if cls != PLAYER_CLASS:
            return cls
        crop = _torso_crop(frame, det["bbox"])
        if official_score(crop) >= self.threshold:
            return OFFICIAL_CLASS
        if self.field_of_play is not None:
            fx, fy = _foot_point(det["bbox"])
            if not _point_in_polygon(fx, fy, self.field_of_play):
                return SIDELINE_CLASS
        return PLAYER_CLASS

    def suppress(
        self, frame: np.ndarray, detections: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return detections with officials/sideline relabeled (copies).

        No detection is dropped. When disabled, returns the input unchanged.
        """
        if not self.enabled:
            return detections
        out: list[dict[str, Any]] = []
        relabeled = 0
        for det in detections:
            new_cls = self.classify(frame, det)
            if new_cls != det.get("class", PLAYER_CLASS):
                det = dict(det)
                det["class"] = new_cls
                relabeled += 1
            out.append(det)
        if relabeled:
            log.debug("official_suppression", relabeled=relabeled, total=len(out))
        return out


def is_non_player(item: dict[str, Any]) -> bool:
    """True if ``item``'s class is an official/sideline (non-player) class.

    Reads ``class`` then ``role``; a missing class is treated as player so
    legacy detections/tracklets with no class field are never filtered.
    """
    explicit_kinds = {"official", "sideline", "unknown"}
    return (
        item.get("class") in explicit_kinds
        or (item.get("provenance") or {}).get("actor_kind") in explicit_kinds
        or item.get("role") in NON_PLAYER_CLASSES
        or item.get("team_label") in NON_PLAYER_CLASSES
    )


def filter_non_players(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop officials/sideline from a detection or tracklet list.

    Backward compatible: items without a class field are kept (treated as
    players), so existing pipelines that don't propagate a class are
    unaffected.
    """
    return [it for it in items if not is_non_player(it)]
