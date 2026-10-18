"""Multi-signal Bayesian snap detector (Issue #132).

The legacy ``_detect_snap`` in ``stage_events.py`` thresholded raw mean bbox
displacement, so it fired on pre-snap shifts, motion men, and even officials
walking through frame. This module replaces it with a naive-Bayes fusion of up
to five *independent* snap signals, following the Oregon State IAAI-2013
formulation:

    logit P(snap) = logit P0 + Σ_i log( P(s_i | snap) / P(s_i | ¬snap) )

with base rate ``P0 = 0.02`` (probability that any single frame inside a play
segment is the snap). Each signal contributes a log-likelihood-ratio scaled by
its continuous strength in ``[0, 1]``; a *missing* signal (no pose, no ball,
no frames) contributes nothing, so the detector degrades gracefully instead of
failing. Every signal is logged per frame for debug / explainability.

Signals (Issue #132 table):

1. VTI optical-flow burst at the LOS band (see ``optical_flow_vti``).
2. OL stance break — torso-angle change > 30° within ≤ 3 frames across ≥ 4 OL.
3. QB hand-under-centre movement — wrist keypoints driving down/back.
4. Centre-ball separation — ball leaves the centre at > 10 yd/s.
5. Formation-stability prior — players static ≥ 0.5 s then break.

No model weights or routed inference: the detector consumes the *outputs* of
``detect`` / ``track`` / ``ball`` / ``pose`` / ``calibrate``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from pipeline.events import optical_flow_vti as vti

log = structlog.get_logger(__name__)

# Base rate: P(a given frame within a play segment is the snap).
BASE_RATE: float = 0.02

# Naive-Bayes weights = log-likelihood-ratio when a signal fires at full
# strength. Tuned so no single signal alone crosses P = 0.5 (forces
# corroboration → low false-positive rate) but any two strong signals do.
LOG_ODDS: dict[str, float] = {
    "vti_burst": 3.0,
    "formation_break": 2.5,
    "ol_stance_break": 3.0,
    "qb_hand_movement": 2.0,
    "center_ball_separation": 3.5,
    # Most of the squad going from set to moving together, on smoothed
    # tracks. The one signal allowed to decide alone, and only when nearly
    # everyone moves at once (strength 1.0 -> P ~0.62 at the base rate); a
    # partial onset still needs corroboration.
    "collective_onset": 4.4,
}

# Decision threshold on the fused probability.
DEFAULT_PROB_THRESHOLD: float = 0.5

# Signal tuning.
STATIC_SPEED_YD_S: float = 1.0  # below this a player is "static"
MOTION_BURST_YD_S: float = 4.0  # snap-grade motion onset
STABLE_SECONDS: float = 0.5  # formation-stability look-back
STANCE_BREAK_DEG: float = 30.0  # OL torso-angle change
STANCE_BREAK_MIN_OL: int = 4  # OL count that must break together
STANCE_BREAK_WINDOW: int = 3  # frames over which the break is measured
CENTER_BALL_SEP_YD_S: float = 10.0  # ball-off-centre separation speed


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _field_track(tracklet: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """Map frame_number → (field_x, field_y) for one tracklet."""
    out: dict[int, tuple[float, float]] = {}
    for pt in tracklet.get("track_points", []):
        fx, fy = pt.get("field_x"), pt.get("field_y")
        fn = pt.get("frame_number")
        if fx is not None and fy is not None and fn is not None:
            out[int(fn)] = (float(fx), float(fy))
    return out


def _torso_angle(kps: dict[str, tuple[float, float]]) -> float | None:
    """Torso orientation (degrees) from shoulder/hip keypoints.

    Uses the vector from the hip midpoint to the shoulder midpoint. Returns
    ``None`` when the required keypoints are absent.
    """
    try:
        ls, rs = kps["left_shoulder"], kps["right_shoulder"]
        lh, rh = kps["left_hip"], kps["right_hip"]
    except KeyError:
        return None
    sx, sy = (ls[0] + rs[0]) / 2, (ls[1] + rs[1]) / 2
    hx, hy = (lh[0] + rh[0]) / 2, (lh[1] + rh[1]) / 2
    return math.degrees(math.atan2(sy - hy, sx - hx))


# ── Individual signal evaluators (independently testable) ───────────────────


def signal_formation_stability(
    tracklets: list[dict[str, Any]],
    fps: float,
    *,
    static_speed_yd_s: float = STATIC_SPEED_YD_S,
    motion_burst_yd_s: float = MOTION_BURST_YD_S,
    stable_seconds: float = STABLE_SECONDS,
) -> dict[int, float]:
    """Strength of "static formation that suddenly breaks" per frame.

    High when the squad was static for ``stable_seconds`` and then bursts into
    motion — the kinematic signature of the snap (Issue #132 signal 5).
    """
    field_tracks = [_field_track(t) for t in tracklets]
    frames = sorted({f for ft in field_tracks for f in ft})
    if len(frames) < 2:
        return {}

    speed: dict[int, float] = {}
    for i in range(1, len(frames)):
        f, prev = frames[i], frames[i - 1]
        dt = (f - prev) / fps if fps else 1.0
        disps: list[float] = []
        for ft in field_tracks:
            if f in ft and prev in ft:
                dx = ft[f][0] - ft[prev][0]
                dy = ft[f][1] - ft[prev][1]
                disps.append(math.hypot(dx, dy) / dt if dt else 0.0)
        speed[f] = float(np.median(disps)) if disps else 0.0

    window = max(1, round(stable_seconds * fps))
    out: dict[int, float] = {}
    for i in range(1, len(frames)):
        f = frames[i]
        look = [frames[j] for j in range(max(1, i - window), i)]
        if not look:
            continue
        stable_frac = sum(1 for g in look if speed.get(g, 0.0) < static_speed_yd_s) / len(look)
        burst = (speed.get(f, 0.0) - static_speed_yd_s) / max(motion_burst_yd_s, 1e-6)
        out[f] = float(np.clip(stable_frac * burst, 0.0, 1.0))
    return out


#: A player is ~2 yards tall: pixel displacement / box height * 2 ~ yards.
PLAYER_HEIGHT_YD: float = 2.0
ONSET_STATIC_YD_S: float = 1.0
ONSET_START_YD_S: float = 1.5
ONSET_MOVING_YD_S: float = 2.5
ONSET_SET_S: float = 0.35
ONSET_CONFIRM_S: float = 0.6
ONSET_CLUSTER_S: float = 0.15
ONSET_SMOOTH_S: float = 0.2


def _positions_yd(tracklet: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """Per-frame position in yards: field coordinates when calibrated, else
    the box's foot point scaled by the track's median box height (players are
    ~2 yd tall), so the signal also works on uncalibrated film. A per-frame
    height would turn box jitter into fake movement."""
    points = tracklet.get("track_points") or []
    out: dict[int, tuple[float, float]] = {}
    heights = [
        float(p["bbox"][3]) - float(p["bbox"][1])
        for p in points
        if p.get("bbox") and len(p["bbox"]) >= 4 and float(p["bbox"][3]) - float(p["bbox"][1]) > 1
    ]
    scale = PLAYER_HEIGHT_YD / float(np.median(heights)) if heights else None
    for point in points:
        frame = point.get("frame_number")
        if frame is None:
            continue
        if point.get("field_x") is not None and point.get("field_y") is not None:
            out[int(frame)] = (float(point["field_x"]), float(point["field_y"]))
            continue
        bbox = point.get("bbox")
        if scale is None or not bbox or len(bbox) < 4:
            continue
        out[int(frame)] = (
            (float(bbox[0]) + float(bbox[2])) / 2 * scale,
            float(bbox[3]) * scale,
        )
    return out


def _smoothed_speeds(positions: dict[int, tuple[float, float]], fps: float) -> dict[int, float]:
    """Speed (yd/s) from a centred moving average, robust to box jitter."""
    frames = sorted(positions)
    if len(frames) < 3:
        return {}
    xs = np.array([positions[f][0] for f in frames])
    ys = np.array([positions[f][1] for f in frames])
    half = max(1, round(ONSET_SMOOTH_S * fps / 2))
    kernel = np.ones(2 * half + 1) / (2 * half + 1)
    if len(frames) > 2 * half + 1:
        xs = np.convolve(np.pad(xs, half, mode="edge"), kernel, mode="valid")
        ys = np.convolve(np.pad(ys, half, mode="edge"), kernel, mode="valid")
    speeds: dict[int, float] = {}
    for i in range(1, len(frames)):
        dt = (frames[i] - frames[i - 1]) / fps if fps else 0.0
        if dt > 0:
            speeds[frames[i]] = float(np.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) / dt)
    return speeds


def _camera_compensated(
    positions: list[dict[int, tuple[float, float]]],
) -> list[dict[int, tuple[float, float]]]:
    """Remove a following camera's motion from pixel-derived positions.

    A pan or drone drift shifts every player the same way; a snap moves them
    in different directions. Subtract the per-frame median displacement of
    all players (accumulated), which is ~zero for field coordinates and the
    camera motion for pixel positions.
    """
    frames = sorted({f for pos in positions for f in pos})
    offset = {frames[0]: (0.0, 0.0)} if frames else {}
    for prev, f in zip(frames, frames[1:], strict=False):
        deltas = [
            (pos[f][0] - pos[prev][0], pos[f][1] - pos[prev][1])
            for pos in positions
            if f in pos and prev in pos
        ]
        dx = float(np.median([d[0] for d in deltas])) if len(deltas) >= 3 else 0.0
        dy = float(np.median([d[1] for d in deltas])) if len(deltas) >= 3 else 0.0
        offset[f] = (offset[prev][0] + dx, offset[prev][1] + dy)
    return [
        {f: (x - offset[f][0], y - offset[f][1]) for f, (x, y) in pos.items()} for pos in positions
    ]


def _player_positions(tracklets: list[dict[str, Any]]) -> list[dict[int, tuple[float, float]]]:
    """Per-player positions in yards, camera-compensated when they come from
    pixels (uncalibrated film) rather than field coordinates."""
    positions = [_positions_yd(t) for t in tracklets]
    calibrated = any(
        p.get("field_x") is not None for t in tracklets for p in (t.get("track_points") or [])
    )
    return positions if calibrated else _camera_compensated(positions)


def player_onsets(tracklets: list[dict[str, Any]], fps: float) -> list[int]:
    """Frame each set player starts moving: the first crossing of 1.5 yd/s
    after >= 0.35 s below 1.0 yd/s, confirmed by reaching 2.5 yd/s within
    0.6 s. Sideline staff and fragments that were never set contribute none.
    """
    onsets: list[int] = []
    set_frames = max(2, round(ONSET_SET_S * fps))
    confirm = round(ONSET_CONFIRM_S * fps)
    for positions in _player_positions(tracklets):
        speeds = _smoothed_speeds(positions, fps)
        frames = sorted(speeds)
        still = 0
        for i, f in enumerate(frames):
            if speeds[f] < ONSET_STATIC_YD_S:
                still += 1
                continue
            if still >= set_frames and speeds[f] >= ONSET_START_YD_S:
                ahead = [speeds[g] for g in frames[i : i + confirm + 1] if g - f <= confirm]
                if ahead and max(ahead) >= ONSET_MOVING_YD_S:
                    onsets.append(f)
                    break
            if speeds[f] >= ONSET_START_YD_S:
                still = 0
    return onsets


def signal_collective_onset(tracklets: list[dict[str, Any]], fps: float) -> dict[int, float]:
    """Strength of "most set players start moving at the same moment".

    Each set player's own movement start (:func:`player_onsets`) is found
    first; the strength at frame ``f`` is how many of those starts fall
    within +/-0.15 s of ``f``, relative to everyone who was set. A snap
    clusters most of them; a motion man or a shift is one or a few. Rises
    from 0 at 15% of set players to 1 at 45% (linemen setting in pass
    protection barely move, so ~40% is a typical snap). Measuring individual starts
    keeps the peak centred on the snap instead of smeared by windows.
    """
    if fps <= 0:
        return {}
    onsets = player_onsets(tracklets, fps)
    if len(onsets) < 4:
        return {}
    speeds = [_smoothed_speeds(pos, fps) for pos in _player_positions(tracklets)]
    set_window = max(2, round(ONSET_SET_S * fps))
    half = max(1, round(ONSET_CLUSTER_S * fps))

    def set_before(frame: int) -> int:
        """Players tracked and still over the ``ONSET_SET_S`` before ``frame``."""
        count = 0
        for sp in speeds:
            window = [v for g, v in sp.items() if frame - half - set_window <= g < frame - half]
            if len(window) >= set_window // 2 and float(np.median(window)) < ONSET_STATIC_YD_S:
                count += 1
        return count

    out: dict[int, float] = {}
    for f in range(min(onsets) - half, max(onsets) + half + 1):
        starting = sum(1 for o in onsets if abs(o - f) <= half)
        if not starting:
            continue
        share = starting / max(set_before(f), starting, 6)
        strength = float(np.clip((share - 0.15) / 0.30, 0.0, 1.0))
        if strength > 0:
            out[f] = strength
    return out


def signal_vti_burst(
    frames: list[np.ndarray],
    frames_start_frame: int,
    *,
    los_band_px: tuple[int, int] | None = None,
    config: vti.VTIConfig | None = None,
) -> dict[int, float]:
    """Strength of the VTI optical-flow burst per absolute frame number."""
    if len(frames) < 2:
        return {}
    counts = vti.active_cell_counts(frames, config=config, los_band_px=los_band_px)
    strengths = vti.burst_strength(counts)
    return {frames_start_frame + i: s for i, s in enumerate(strengths) if s > 0.0}


def signal_ol_stance_break(
    pose_by_frame: dict[int, dict[str, dict[str, tuple[float, float]]]],
    ol_track_ids: list[str],
    *,
    break_deg: float = STANCE_BREAK_DEG,
    min_ol: int = STANCE_BREAK_MIN_OL,
    window: int = STANCE_BREAK_WINDOW,
) -> dict[int, float]:
    """Strength of a simultaneous OL torso-angle break per frame (signal 2)."""
    if not ol_track_ids:
        return {}
    ol = set(ol_track_ids)
    # Per OL track, frame → torso angle.
    angles: dict[str, dict[int, float]] = {tid: {} for tid in ol}
    for frame, players in pose_by_frame.items():
        for tid, kps in players.items():
            if tid in ol:
                a = _torso_angle(kps)
                if a is not None:
                    angles[tid][frame] = a

    frames = sorted(pose_by_frame)
    out: dict[int, float] = {}
    for i, f in enumerate(frames):
        ref_idx = max(0, i - window)
        ref_f = frames[ref_idx]
        broke = 0
        for tid in ol:
            a_now = angles[tid].get(f)
            a_ref = angles[tid].get(ref_f)
            if a_now is None or a_ref is None:
                continue
            delta = abs((a_now - a_ref + 180) % 360 - 180)
            if delta > break_deg:
                broke += 1
        if broke:
            out[f] = float(np.clip(broke / min_ol, 0.0, 1.0))
    return out


def signal_qb_hand_movement(
    pose_by_frame: dict[int, dict[str, dict[str, tuple[float, float]]]],
    qb_track_id: str | None,
    fps: float,
    *,
    velocity_threshold_px_s: float = 200.0,
) -> dict[int, float]:
    """Strength of the QB's hands driving down/back per frame (signal 3).

    Under-centre snaps show the QB's wrists accelerating downward (image +y)
    to receive the ball. Shotgun snaps lack this — the signal simply stays low
    and the other signals carry the detection.
    """
    if qb_track_id is None:
        return {}
    frames = sorted(pose_by_frame)

    def wrist(frame: int) -> tuple[float, float] | None:
        kps = pose_by_frame.get(frame, {}).get(qb_track_id)
        if not kps:
            return None
        ws = [kps[k] for k in ("left_wrist", "right_wrist") if k in kps]
        if not ws:
            return None
        return (sum(w[0] for w in ws) / len(ws), sum(w[1] for w in ws) / len(ws))

    out: dict[int, float] = {}
    for i in range(1, len(frames)):
        f, prev = frames[i], frames[i - 1]
        cur, pre = wrist(f), wrist(prev)
        if cur is None or pre is None:
            continue
        dt = (f - prev) / fps if fps else 1.0
        if dt <= 0:
            continue
        vy = (cur[1] - pre[1]) / dt  # downward positive
        if vy <= 0:
            continue
        out[f] = float(np.clip(vy / max(velocity_threshold_px_s, 1e-6), 0.0, 1.0))
    return out


def signal_center_ball_separation(
    ball_track: dict[int, tuple[float, float]],
    center_track: dict[int, tuple[float, float]],
    fps: float,
    *,
    separation_speed_yd_s: float = CENTER_BALL_SEP_YD_S,
) -> dict[int, float]:
    """Strength of the ball separating from the centre per frame (signal 4)."""
    if not ball_track or not center_track:
        return {}
    frames = sorted(set(ball_track) & set(center_track))
    out: dict[int, float] = {}
    for i in range(1, len(frames)):
        f, prev = frames[i], frames[i - 1]
        if prev not in ball_track or prev not in center_track:
            continue
        dt = (f - prev) / fps if fps else 1.0
        if dt <= 0:
            continue
        bx, by = ball_track[f]
        cx, cy = center_track[f]
        pbx, pby = ball_track[prev]
        pcx, pcy = center_track[prev]
        sep_now = math.hypot(bx - cx, by - cy)
        sep_prev = math.hypot(pbx - pcx, pby - pcy)
        rate = (sep_now - sep_prev) / dt
        if rate <= 0:
            continue
        out[f] = float(np.clip(rate / max(separation_speed_yd_s, 1e-6), 0.0, 1.0))
    return out


# ── Inputs / result containers ──────────────────────────────────────────────


@dataclass
class SnapInputs:
    """Everything the snap detector can consume; every field is optional.

    Field coordinates are NCAA-template yards (Issue #127). Pose is keyed
    ``frame → track_id → keypoint_name → (x, y)`` in image pixels.
    """

    tracklets: list[dict[str, Any]] = field(default_factory=list)
    fps: float = 30.0
    frames: list[np.ndarray] | None = None
    frames_start_frame: int = 0
    los_band_px: tuple[int, int] | None = None
    pose_by_frame: dict[int, dict[str, dict[str, tuple[float, float]]]] | None = None
    ol_track_ids: list[str] | None = None
    qb_track_id: str | None = None
    ball_track: dict[int, tuple[float, float]] | None = None
    center_track: dict[int, tuple[float, float]] | None = None


@dataclass
class SnapResult:
    """Output of :meth:`BayesianSnapDetector.detect`."""

    snap_frame: int | None
    confidence: float
    frame_probabilities: dict[int, float]
    signal_strengths: dict[int, dict[str, float]]
    signals_present: list[str]

    def explain(self, frame: int) -> dict[str, float]:
        """Per-signal strengths that drove the probability at ``frame``."""
        return self.signal_strengths.get(frame, {})


class BayesianSnapDetector:
    """Fuse the five snap signals into a calibrated per-frame ``P(snap)``."""

    def __init__(
        self,
        *,
        base_rate: float = BASE_RATE,
        log_odds: dict[str, float] | None = None,
        prob_threshold: float = DEFAULT_PROB_THRESHOLD,
    ) -> None:
        self.base_rate = base_rate
        self.log_odds = dict(log_odds or LOG_ODDS)
        self.prob_threshold = prob_threshold

    def detect(self, inputs: SnapInputs) -> SnapResult:
        signals: dict[str, dict[int, float]] = {}

        signals["formation_break"] = signal_formation_stability(inputs.tracklets, inputs.fps)
        signals["collective_onset"] = signal_collective_onset(inputs.tracklets, inputs.fps)
        if inputs.frames:
            signals["vti_burst"] = signal_vti_burst(
                inputs.frames,
                inputs.frames_start_frame,
                los_band_px=inputs.los_band_px,
            )
        if inputs.pose_by_frame and inputs.ol_track_ids:
            signals["ol_stance_break"] = signal_ol_stance_break(
                inputs.pose_by_frame, inputs.ol_track_ids
            )
        if inputs.pose_by_frame and inputs.qb_track_id is not None:
            signals["qb_hand_movement"] = signal_qb_hand_movement(
                inputs.pose_by_frame, inputs.qb_track_id, inputs.fps
            )
        if inputs.ball_track and inputs.center_track:
            signals["center_ball_separation"] = signal_center_ball_separation(
                inputs.ball_track, inputs.center_track, inputs.fps
            )

        signals = {name: s for name, s in signals.items() if s}
        signals_present = sorted(signals)

        base_logit = _logit(self.base_rate)
        all_frames = sorted({f for s in signals.values() for f in s})
        probabilities: dict[int, float] = {}
        strengths: dict[int, dict[str, float]] = {}
        for f in all_frames:
            logit = base_logit
            per_frame: dict[str, float] = {}
            for name, series in signals.items():
                strength = series.get(f, 0.0)
                if strength > 0.0:
                    per_frame[name] = round(strength, 4)
                    logit += self.log_odds.get(name, 0.0) * strength
            probabilities[f] = round(_sigmoid(logit), 6)
            if per_frame:
                strengths[f] = per_frame

        snap_frame: int | None = None
        confidence = 0.0
        for f in all_frames:
            p = probabilities[f]
            if p >= self.prob_threshold and p > confidence:
                snap_frame, confidence = f, p
        if snap_frame is None and probabilities:
            confidence = max(probabilities.values())
        if snap_frame is not None and "collective_onset" in signals:
            # Centre the snap on where the set players' own starts cluster
            # (median of the starts near the decision frame).
            near = [
                o
                for o in player_onsets(inputs.tracklets, inputs.fps)
                if abs(o - snap_frame) <= round(0.5 * inputs.fps)
            ]
            if near:
                snap_frame = int(np.median(near))

        log.info(
            "snap_detect",
            snap_frame=snap_frame,
            confidence=round(confidence, 4),
            signals_present=signals_present,
        )
        return SnapResult(
            snap_frame=snap_frame,
            confidence=confidence,
            frame_probabilities=probabilities,
            signal_strengths=strengths,
            signals_present=signals_present,
        )
