"""Tests for the multi-signal Bayesian snap detector (Issue #132)."""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.events.snap_detector import (
    BASE_RATE,
    BayesianSnapDetector,
    SnapInputs,
    signal_center_ball_separation,
    signal_formation_stability,
    signal_ol_stance_break,
)

FPS = 30.0
SNAP = 10


def _static_then_move(tid: str, fx0: float, fy0: float) -> dict[str, Any]:
    pts = []
    for f in range(0, 25):
        # Static through SNAP-1, then drift forward each frame.
        fx = fx0 + (0.0 if f < SNAP else 0.4 * (f - SNAP + 1))
        pts.append(
            {
                "frame_number": f,
                "field_x": fx,
                "field_y": fy0,
                "bbox": [fx0 * 10, fy0 * 10, fx0 * 10 + 5, fy0 * 10 + 5],
            }
        )
    return {"id": tid, "track_points": pts}


def _formation(n: int = 11) -> list[dict[str, Any]]:
    return [_static_then_move(f"p{i}", 50.0, float(i - n // 2)) for i in range(n)]


def _ol_pose() -> dict[int, dict[str, dict[str, tuple[float, float]]]]:
    """4 OL whose torso angle snaps >30° at frame SNAP and holds."""
    pose: dict[int, dict[str, dict[str, tuple[float, float]]]] = {}
    for f in range(0, 25):
        broken = f >= SNAP
        frame_players: dict[str, dict[str, tuple[float, float]]] = {}
        for i in range(4):
            tid = f"p{i}"
            # Pre-snap: upright (shoulders above hips). Post-snap: lurch forward.
            sx = 100.0 + (40.0 if broken else 0.0)
            frame_players[tid] = {
                "left_shoulder": (sx - 5, 50.0),
                "right_shoulder": (sx + 5, 50.0),
                "left_hip": (100.0 - 5, 100.0),
                "right_hip": (100.0 + 5, 100.0),
            }
        pose[f] = frame_players
    return pose


# ── Individual signal evaluators ────────────────────────────────────────────


def test_formation_stability_fires_at_snap() -> None:
    s = signal_formation_stability(_formation(), FPS)
    assert s, "expected non-empty signal series"
    peak = max(s, key=lambda f: s[f])
    assert SNAP - 2 <= peak <= SNAP + 2
    assert s[peak] > 0.0


def test_ol_stance_break_requires_min_ol() -> None:
    s = signal_ol_stance_break(_ol_pose(), ["p0", "p1", "p2", "p3"])
    assert s
    assert max(s.values()) >= 1.0  # all 4 OL broke ⇒ full strength


def test_no_ol_ids_yields_empty() -> None:
    assert signal_ol_stance_break(_ol_pose(), []) == {}


def test_center_ball_separation() -> None:
    ball = {SNAP - 1: (50.0, 0.0), SNAP: (50.0, 0.0), SNAP + 1: (49.0, -1.0)}
    center = {SNAP - 1: (50.0, 0.0), SNAP: (50.0, 0.0), SNAP + 1: (50.0, 0.0)}
    s = signal_center_ball_separation(ball, center, FPS)
    assert s.get(SNAP + 1, 0.0) > 0.0


# ── Fused detector ──────────────────────────────────────────────────────────


def test_two_signals_detect_snap() -> None:
    inputs = SnapInputs(
        tracklets=_formation(),
        fps=FPS,
        pose_by_frame=_ol_pose(),
        ol_track_ids=["p0", "p1", "p2", "p3"],
    )
    result = BayesianSnapDetector().detect(inputs)
    assert result.snap_frame is not None
    assert SNAP - 2 <= result.snap_frame <= SNAP + 2
    assert result.confidence > 0.5
    assert "formation_break" in result.signals_present
    assert "ol_stance_break" in result.signals_present
    # Explainability: per-signal strengths recorded at the snap frame.
    assert result.explain(result.snap_frame)


def test_single_weak_signal_no_false_snap() -> None:
    # Only one player (a motion man) moves; the median-based formation signal
    # stays quiet and no snap should be asserted.
    tracks = [_static_then_move("p0", 50.0, 0.0)]
    tracks += [
        {
            "id": f"p{i}",
            "track_points": [
                {"frame_number": f, "field_x": 50.0, "field_y": float(i)} for f in range(25)
            ],
        }
        for i in range(1, 11)
    ]
    result = BayesianSnapDetector().detect(SnapInputs(tracklets=tracks, fps=FPS))
    assert result.snap_frame is None
    assert result.confidence < 0.5


def test_no_signals_returns_none() -> None:
    result = BayesianSnapDetector().detect(SnapInputs(tracklets=[], fps=FPS))
    assert result.snap_frame is None
    assert result.signals_present == []


def test_base_rate_is_low() -> None:
    assert 0.0 < BASE_RATE < 0.1


def _set_then_go(
    tid: str, x0: float, y0: float, go: int, *, drift: float = 0.0, angle: float = 0.0
) -> dict[str, Any]:
    """Pixel-only track (no calibration): set, then moving from frame ``go``
    in direction ``angle`` (radians). ``drift`` adds a following camera's pan
    to every frame. Players scatter in different directions at a real snap.
    """
    import math

    pts = []
    for f in range(0, 75):
        # ~2 yd (40 px) boxes: reaches ~4 yd/s within a third of a second.
        run = 0.0 if f < go else 1.5 * (f - go + 1) ** 1.3
        x = x0 + run * math.cos(angle) + drift * f
        y = y0 + run * math.sin(angle)
        pts.append({"frame_number": f, "bbox": [x - 10, y - 40, x + 10, y]})
    return {"id": tid, "track_points": pts}


def test_collective_onset_finds_snap_on_uncalibrated_panning_film() -> None:
    from pipeline.events.snap_detector import player_onsets

    snap = 30
    # 12 set players go at the snap; a following camera pans 1.5 px/frame.
    tracks = [
        _set_then_go(f"p{i}", 100.0 + 40 * i, 300.0, snap, drift=1.5, angle=i * 0.52)
        for i in range(12)
    ]
    # Sideline staff never set: walking the whole time.
    tracks.append(
        {
            "id": "coach",
            "track_points": [
                {"frame_number": f, "bbox": [10 + 3 * f, 260, 30 + 3 * f, 300]} for f in range(75)
            ],
        }
    )
    onsets = player_onsets(tracks, FPS)
    assert len(onsets) >= 10
    result = BayesianSnapDetector().detect(SnapInputs(tracklets=tracks, fps=FPS))
    assert result.snap_frame is not None
    assert abs(result.snap_frame - snap) <= 4
    assert "collective_onset" in result.signals_present


def test_one_motion_man_is_not_a_snap() -> None:
    tracks = [_set_then_go("motion", 100.0, 300.0, 20)]
    tracks += [_set_then_go(f"p{i}", 200.0 + 40 * i, 300.0, 10_000) for i in range(11)]
    result = BayesianSnapDetector().detect(SnapInputs(tracklets=tracks, fps=FPS))
    assert result.snap_frame is None
