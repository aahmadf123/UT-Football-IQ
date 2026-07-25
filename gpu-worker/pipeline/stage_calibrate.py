"""Stage 3 — Regime-aware field calibration (Issues #127, #138).

Replaces the original 4-corner stub homography with a real yard-line
calibration that branches on the capture regime detected at ingest
(Issue #126):

* **FIXED_SIDELINE** (game film) — the elevated camera is effectively
  bolted down, so a single homography is fit once on the cleanest frame and
  flagged ``is_game_anchor``. Same-session jobs can reuse the cached anchor;
  this stage produces it.
* **DRONE_FOLLOW / unknown** (practice film) — the operator pans/zooms, so
  the clip is calibrated per window. Nightly jobs additionally Kalman-smooth
  the per-window homographies and use chained-ECC drift (Issue #138) as the
  temporal-stability signal.

Shared math core (all pixel-only, single-camera, no GPS/IMU/SRT):
  white-paint detection → Hough → angle clustering → labeled yard-line
  correspondences → normalized DLT + RANSAC → 5-component confidence.

Coordinate system (NCAA template, see ``homography/field_template.py``):
  X: 0–100 yards (goal line to goal line)
  Y: -26.665 to +26.665 yards (south sideline to north sideline)

Output: a ``field_calibrations`` row with the homography, the blended
confidence, the five confidence sub-components, the per-regime diagnostics
(inlier_ratio, line_count, parallel_variance, temporal_drift), the Kalman
state, ``is_game_anchor``, and ``analytics_safe``.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from pipeline import backend, r2
from pipeline.homography import camera_motion_ecc as ecc
from pipeline.homography import confidence_scorer as cs
from pipeline.homography import dlt_ransac, kalman_smoother
from pipeline.homography import yardline_keypoints as yk

log = structlog.get_logger(__name__)

CONFIDENCE_THRESHOLD = 0.75   # Issue #127 gate: analytics_safe at ≥ 0.75
RANSAC_THRESHOLD_PX = 3.0     # Issue #127 §7.1 re-projection threshold
SAMPLE_INTERVAL_S = 5.0
N_SAMPLE_FRAMES_FIXED = 6     # fixed camera: pick the single cleanest frame
N_SAMPLE_FRAMES_DRONE = 12    # drone: sample more for a per-window series

# Chained-ECC drift (Issue #138 §5.1): for DRONE_FOLLOW we additionally pull a
# *consecutive* window of frames and compose inter-frame ECC warps onto the
# sparsely-refit anchors. The mean chained-vs-direct drift over this window is
# the temporal-stability signal (target < 2 px over ~5 s).
ECC_WINDOW_S = 5.0            # length of the consecutive ECC window (seconds)
ECC_MAX_FRAMES = 30           # cap frames read for ECC (subsample if needed)

# Routing variants registered for the ``calibrate`` stage (model_router).
VARIANT_LITE = "calib-hough-dlt"          # same-session: Hough + DLT, no Kalman
VARIANT_KALMAN = "calib-hough-dlt-kalman"  # nightly: + Kalman temporal smoothing

FIXED_SIDELINE = "fixed_sideline"
DRONE_FOLLOW = "drone_follow"
UNKNOWN = "unknown"


def run(
    video_id: str,
    input_uri: str,
    job_id: str,
    *,
    variant: str = VARIANT_KALMAN,
    capture_regime: str | None = None,
) -> dict[str, Any]:
    """Run Stage 3 for the given clip and return output artifacts.

    Args:
        capture_regime: regime from ingest (``fixed_sideline`` /
            ``drone_follow`` / ``unknown``). ``None`` is treated as
            ``unknown`` and uses the per-window drone path.
        variant: model-router variant. ``calib-hough-dlt-kalman`` enables
            Kalman smoothing of the per-window series (nightly); the lite
            variant skips it (same-session).
    """
    regime = capture_regime or UNKNOWN
    log.info(
        "stage_calibrate_start",
        video_id=video_id,
        capture_regime=regime,
        variant=variant,
    )

    r2_key = _uri_to_r2_key(input_uri)
    video_path = r2.download_to_temp(r2_key)
    try:
        return _calibrate(video_id, video_path, job_id, regime, variant)
    finally:
        video_path.unlink(missing_ok=True)


def _uri_to_r2_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


def _calibrate(
    video_id: str,
    video_path: Path,
    job_id: str,
    regime: str,
    variant: str,
) -> dict[str, Any]:
    n_frames = N_SAMPLE_FRAMES_FIXED if regime == FIXED_SIDELINE else N_SAMPLE_FRAMES_DRONE
    frames = _sample_frames(video_path, n_frames)
    if not frames:
        return _persist_and_return(
            video_id, job_id,
            homography=None, breakdown=None, regime=regime,
            inlier_ratio=0.0, line_count=0, parallel_variance=None,
            temporal_drift=None, kalman_state=None, is_game_anchor=False,
            reason_codes=["no_frames"],
        )

    if regime == FIXED_SIDELINE:
        return _calibrate_fixed_sideline(video_id, job_id, frames, regime)
    return _calibrate_drone(
        video_id, job_id, frames, regime, variant, video_path=video_path
    )


# ── FIXED_SIDELINE: one anchor homography for the whole clip ───────────────────


def _calibrate_fixed_sideline(
    video_id: str, job_id: str, frames: list[np.ndarray], regime: str
) -> dict[str, Any]:
    """Fit a single homography on the cleanest frame and flag it as the anchor."""
    best = _best_frame_fit(frames)
    if best is None:
        return _persist_and_return(
            video_id, job_id,
            homography=None, breakdown=None, regime=regime,
            inlier_ratio=0.0, line_count=0, parallel_variance=None,
            temporal_drift=None, kalman_state=None, is_game_anchor=False,
            reason_codes=["no_calibration"],
        )
    H, kp, inlier_ratio, reason_codes = best
    parallel = cs.parallel_line_score(kp.yardline_angles)
    # A single bolted-down anchor has no temporal drift by construction.
    breakdown = cs.compute_confidence(
        inlier_ratio=inlier_ratio,
        line_count=kp.line_count,
        parallel_line_score=parallel,
        temporal_stability=cs.temporal_stability_from_drift(0.0),
        field_coverage=kp.field_coverage,
    )
    return _persist_and_return(
        video_id, job_id,
        homography=H, breakdown=breakdown, regime=regime,
        inlier_ratio=inlier_ratio, line_count=kp.line_count,
        parallel_variance=_variance(kp.yardline_angles),
        temporal_drift=0.0, kalman_state=None, is_game_anchor=True,
        reason_codes=reason_codes,
    )


# ── DRONE_FOLLOW / unknown: per-window series + optional Kalman ────────────────


def _calibrate_drone(
    video_id: str,
    job_id: str,
    frames: list[np.ndarray],
    regime: str,
    variant: str,
    *,
    video_path: Path | None = None,
) -> dict[str, Any]:
    """Per-window calibration; nightly variant adds Kalman temporal smoothing.

    ``video_path`` is optional: when omitted, chained-ECC is treated as
    unavailable and the temporal-stability signal falls back to ``series_drift``.
    """
    fits: list[tuple[np.ndarray, yk.KeypointResult, float] | None] = []
    for frame in frames:
        best = _best_frame_fit([frame])
        if best is None:
            fits.append(None)
            continue
        H, kp, inlier_ratio, _ = best
        fits.append((H, kp, inlier_ratio))

    valid = [f for f in fits if f is not None]
    if not valid:
        return _persist_and_return(
            video_id, job_id,
            homography=None, breakdown=None, regime=regime,
            inlier_ratio=0.0, line_count=0, parallel_variance=None,
            temporal_drift=None, kalman_state=None, is_game_anchor=False,
            reason_codes=["no_calibration"],
        )

    homographies = [f[0] for f in valid]
    confidences = [f[2] for f in valid]
    shape = frames[0].shape[:2]
    # Temporal drift: prefer the chained-ECC signal over a *consecutive* window
    # (Issue #138 §5.1) — it tracks pan/zoom drift between sparse anchors. Fall
    # back to the per-window pairwise re-projection gap when ECC is unavailable
    # (no OpenCV / too-short clip). Low drift ⇒ stable.
    series_drift = _series_drift(homographies, shape)
    if video_path is not None:
        ecc_drift, ecc_diag = _chained_ecc_drift(video_path, regime, shape)
    else:
        # No clip on disk ⇒ ECC unavailable; fall back to the series drift.
        ecc_drift, ecc_diag = None, {}
    temporal_drift = ecc_drift if ecc_drift is not None else series_drift
    temporal_stability = cs.temporal_stability_from_drift(temporal_drift)
    ecc_diag = {"series_drift_px": round(float(series_drift), 4), **ecc_diag}

    kalman_state: list[float] | None = None
    chosen_H = homographies[len(homographies) // 2]  # median-index window
    use_kalman = variant == VARIANT_KALMAN
    if use_kalman and len(homographies) >= 2:
        kf = kalman_smoother.HomographyKalman(
            sigma_q=kalman_smoother.process_noise_for_regime(regime)
        )
        smoothed = None
        for H, conf in zip(homographies, confidences):
            smoothed = kf.update(H, conf)
        if smoothed is not None:
            chosen_H = smoothed
        kalman_state = kf.state_vector()

    # Representative diagnostics from the strongest window.
    best_idx = int(np.argmax(confidences))
    best_kp = valid[best_idx][1]
    best_inlier = valid[best_idx][2]
    parallel = cs.parallel_line_score(best_kp.yardline_angles)
    breakdown = cs.compute_confidence(
        inlier_ratio=best_inlier,
        line_count=best_kp.line_count,
        parallel_line_score=parallel,
        temporal_stability=temporal_stability,
        field_coverage=best_kp.field_coverage,
    )
    return _persist_and_return(
        video_id, job_id,
        homography=chosen_H, breakdown=breakdown, regime=regime,
        inlier_ratio=best_inlier, line_count=best_kp.line_count,
        parallel_variance=_variance(best_kp.yardline_angles),
        temporal_drift=temporal_drift, kalman_state=kalman_state,
        is_game_anchor=False, reason_codes=list(best_kp.reason_codes),
        extra_diagnostics=ecc_diag,
    )


# ── Shared single-frame fit ────────────────────────────────────────────────────


def _best_frame_fit(
    frames: list[np.ndarray],
) -> tuple[np.ndarray, yk.KeypointResult, float, list[str]] | None:
    """Detect keypoints + fit a RANSAC homography on the best of ``frames``.

    Returns ``(H, KeypointResult, inlier_ratio, reason_codes)`` for the frame
    that yields the most inliers, or ``None`` if no frame produces a fit.
    """
    best: tuple[np.ndarray, yk.KeypointResult, float, list[str]] | None = None
    best_inliers = -1
    for frame in frames:
        kp = yk.detect_keypoints(frame)
        if not kp.has_enough():
            continue
        H, mask = dlt_ransac.ransac_homography(
            kp.src_pts, kp.dst_pts, threshold=RANSAC_THRESHOLD_PX
        )
        if H is None:
            continue
        n_in = int(np.count_nonzero(mask))
        inlier_ratio = n_in / max(len(mask), 1)
        if n_in > best_inliers:
            best_inliers = n_in
            reason_codes = list(kp.reason_codes)
            if inlier_ratio < 0.5:
                reason_codes.append("low_inlier_ratio")
            best = (H, kp, inlier_ratio, reason_codes)
    return best


# ── Helpers ─────────────────────────────────────────────────────────────────


def _series_drift(homographies: list[np.ndarray], shape: tuple[int, int]) -> float:
    """Mean pairwise re-projection gap (px) across consecutive homographies."""
    if len(homographies) < 2:
        return 0.0
    h, w = shape
    corners = np.array(
        [[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64
    )
    gaps: list[float] = []
    for a, b in zip(homographies[:-1], homographies[1:]):
        gaps.append(_reproj_gap(a, b, corners))
    return float(np.mean(gaps)) if gaps else 0.0


def _reproj_gap(H_a: np.ndarray, H_b: np.ndarray, pts: np.ndarray) -> float:
    homog = np.hstack([pts, np.ones((len(pts), 1))])
    try:
        inv_b = np.linalg.inv(H_b)
    except np.linalg.LinAlgError:
        inv_b = np.linalg.pinv(H_b)
    rel = inv_b @ H_a
    warped = (rel @ homog.T).T
    denom = np.where(np.abs(warped[:, 2:3]) < 1e-12, 1e-12, warped[:, 2:3])
    warped_xy = warped[:, :2] / denom
    return float(np.sqrt(((warped_xy - pts) ** 2).sum(axis=1)).mean())


def _variance(angles: list[float]) -> float | None:
    if len(angles) < 2:
        return None
    return float(np.var([float(a) for a in angles]))


# ── Chained-ECC temporal drift (Issue #138 §5.1) ──────────────────────────────


def _chained_ecc_drift(
    video_path: Path, regime: str, shape: tuple[int, int]
) -> tuple[float | None, dict[str, Any]]:
    """Mean chained-vs-direct re-projection drift over a consecutive window.

    Pulls a ~5 s consecutive window, composes inter-frame ECC warps onto the
    sparsely-refit anchors (:func:`camera_motion_ecc.compensate_sequence`), and
    returns the mean checkpoint drift plus diagnostics. Returns ``(None, …)``
    when ECC can't run (no OpenCV / too-short window / no anchorable frame), so
    the caller falls back to the per-window series drift.
    """
    window, eff_fps = _sample_window_frames(video_path, ECC_WINDOW_S, ECC_MAX_FRAMES)
    if len(window) < 3:
        return None, {}

    grays = [ecc.to_gray(f) for f in window]
    masks = [ecc.field_background_mask(f) for f in window]
    h, w = shape
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)

    def warp_at(i: int) -> tuple[np.ndarray, bool]:
        # ECC template is the previous frame, so mask the previous frame too.
        return ecc.estimate_warp(grays[i - 1], grays[i], mask=masks[i - 1])

    anchor_cache: dict[int, tuple[np.ndarray, float] | None] = {}

    def anchor_fit(i: int) -> tuple[np.ndarray, float] | None:
        # compensate_sequence may request the same index repeatedly (seed scan,
        # checkpoints, re-anchors); cache the keypoint/RANSAC fit per frame.
        if i in anchor_cache:
            return anchor_cache[i]
        best = _best_frame_fit([window[i]])
        if best is None:
            anchor_cache[i] = None
            return None
        H, _kp, inlier_ratio, _ = best
        fit = (H, float(inlier_ratio))
        anchor_cache[i] = fit
        return fit

    result = ecc.compensate_sequence(
        n_frames=len(window),
        regime=regime,
        fps=eff_fps,
        anchor_fit=anchor_fit,
        warp_at=warp_at,
        sample_pts=corners,
    )
    diag: dict[str, Any] = {
        "ecc_anchored": result.anchored,
        "ecc_reanchors": result.reanchor_count,
        "ecc_zoom_breach": result.zoom_breach,
        "ecc_window_frames": result.n_frames,
        "ecc_fps": round(float(eff_fps), 3),
    }
    if not result.anchored or result.n_checks == 0:
        return None, diag
    diag["ecc_mean_drift_px"] = round(result.mean_drift_px, 4)
    diag["ecc_max_drift_px"] = round(result.max_drift_px, 4)
    diag["ecc_checks"] = result.n_checks
    return result.mean_drift_px, diag


def _sample_window_frames(
    video_path: Path, window_s: float, max_frames: int
) -> tuple[list[np.ndarray], float]:
    """Read a *consecutive* run of BGR frames over a centered ``window_s`` span.

    Subsamples by a stride so at most ``max_frames`` are returned, and reports
    the *effective* fps (native / stride) so per-second zoom math stays correct.
    Returns ``([], fps)`` on any failure (treated as "ECC unavailable").
    """
    try:
        import cv2
    except Exception:
        return [], 30.0
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return [], 30.0
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if total <= 0 or fps <= 0:
            return [], 30.0
        window_n = min(int(round(window_s * fps)), total)
        if window_n <= 1:
            return [], fps
        start = max(0, (total - window_n) // 2)
        stride = max(1, math.ceil(window_n / max_frames))
        cap.set(cv2.CAP_PROP_POS_FRAMES, float(start))
        frames: list[np.ndarray] = []
        for grabbed in range(window_n):
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if grabbed % stride == 0:
                frames.append(frame)
                if len(frames) >= max_frames:
                    break
        return frames, fps / stride
    finally:
        cap.release()


def _sample_frames(video_path: Path, n: int) -> list[np.ndarray]:
    """Extract up to ``n`` evenly spaced BGR frames; empty list on failure."""
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


def _persist_and_return(
    video_id: str,
    job_id: str,
    *,
    homography: np.ndarray | None,
    breakdown: cs.ConfidenceBreakdown | None,
    regime: str,
    inlier_ratio: float,
    line_count: int,
    parallel_variance: float | None,
    temporal_drift: float | None,
    kalman_state: list[float] | None,
    is_game_anchor: bool,
    reason_codes: list[str],
    extra_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the calibration record and return the stage output artifacts."""
    confidence = breakdown.confidence if breakdown is not None else 0.0
    components = breakdown.as_dict() if breakdown is not None else {}
    # Disqualifying reason codes block analytics regardless of score.
    blocking = {
        "no_frames", "no_calibration", "cv2_unavailable",
        "insufficient_lines", "insufficient_structured_lines",
        "insufficient_yard_lines", "insufficient_intersections",
    }
    has_blocking = any(rc in blocking for rc in reason_codes)
    analytics_safe = (confidence >= CONFIDENCE_THRESHOLD) and not has_blocking

    homography_list = (
        [float(v) for v in np.asarray(homography, dtype=np.float64).flatten()]
        if homography is not None
        else None
    )
    calibration_points: dict[str, Any] = {
        "capture_regime": regime,
        "confidence_components": components,
    }
    if extra_diagnostics:
        calibration_points["motion_compensation"] = extra_diagnostics

    try:
        backend.create_calibration(
            video_id,
            homography=homography_list,
            confidence=float(confidence),
            confidence_threshold=CONFIDENCE_THRESHOLD,
            analytics_safe=analytics_safe,
            reason_codes=reason_codes or None,
            calibration_points=calibration_points,
            inlier_ratio=float(inlier_ratio),
            line_count=int(line_count),
            parallel_variance=parallel_variance,
            temporal_drift=temporal_drift,
            kalman_state=kalman_state,
            is_game_anchor=is_game_anchor,
            job_id=job_id,
        )
    except Exception as exc:
        log.error("write_calibration_failed", video_id=video_id, error=str(exc))

    log.info(
        "stage_calibrate_done",
        video_id=video_id,
        capture_regime=regime,
        confidence=round(float(confidence), 4),
        analytics_safe=analytics_safe,
        is_game_anchor=is_game_anchor,
    )
    return {
        "analytics_safe": analytics_safe,
        "confidence": float(confidence),
        "capture_regime": regime,
        "confidence_components": components,
        "is_game_anchor": is_game_anchor,
        "reason_codes": reason_codes,
    }
