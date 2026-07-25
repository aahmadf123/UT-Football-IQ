"""Tests for the BoT-SORT tracker adapter (Issue #129).

Covers the AbstractTracker (``TrackerBase``) contract, track continuity, the
constant-velocity + camera-motion prediction, optional appearance scoring, and
graceful degradation to a pure motion/IoU tracker when no extras are supplied.
Pure NumPy — no torch, no weights, no GPU.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.tracker_models import TrackerBase, get_tracker
from pipeline.tracking.botsort_adapter import BoTSORTTracker, warp_bbox


def _det(x: float, y: float, embedding: list[float] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"bbox": [x, y, x + 30, y + 60], "confidence": 0.9, "class": "player"}
    if embedding is not None:
        d["embedding"] = embedding
    return d


# ── Contract ──────────────────────────────────────────────────────────────────


def test_botsort_is_tracker_base_subclass() -> None:
    assert issubclass(BoTSORTTracker, TrackerBase)
    assert isinstance(BoTSORTTracker(), TrackerBase)


def test_factory_returns_botsort() -> None:
    assert isinstance(get_tracker("botsort"), BoTSORTTracker)


def test_track_points_always_have_frame_and_bbox() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(102, 101)]}
    for track in BoTSORTTracker().track(detections):
        for pt in track.points:
            assert "frame_number" in pt
            assert "bbox" in pt and len(pt["bbox"]) == 4


# ── Continuity ──────────────────────────────────────────────────────────────


def test_single_track_continues_across_frames() -> None:
    detections = {
        "0": [_det(100, 100)],
        "1": [_det(102, 101)],
        "2": [_det(104, 102)],
    }
    results = BoTSORTTracker(iou_threshold=0.3, max_lost_frames=5).track(detections)
    assert len(results) == 1
    assert [p["frame_number"] for p in results[0].points] == [0, 1, 2]


def test_far_detection_starts_new_track() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(102, 101), _det(800, 100)]}
    assert len(BoTSORTTracker(max_lost_frames=10).track(detections)) == 2


def test_ignores_non_player_detections() -> None:
    detections = {
        "0": [_det(100, 100), {"bbox": [50, 50, 60, 60], "class": "ball"}],
    }
    results = BoTSORTTracker().track(detections)
    assert len(results) == 1


def test_constant_velocity_bridges_a_detection_gap() -> None:
    # Player moves 15 px/frame; the detector misses frame 2. The learned
    # velocity lets the predicted box land on the reappearing frame-3 box, so
    # the identity survives the gap. Pure IoU (see below) would split it.
    detections = {
        "0": [_det(100, 100)],
        "1": [_det(115, 100)],
        "3": [_det(145, 100)],  # frame 2 missing
    }
    results = BoTSORTTracker(iou_threshold=0.2, max_lost_frames=5).track(detections)
    assert len(results) == 1
    assert [p["frame_number"] for p in results[0].points] == [0, 1, 3]


def test_pure_iou_would_split_the_gap() -> None:
    # Contrast: the legacy IoU tracker (no motion model) loses the gap above.
    from pipeline.tracker_models import IoUTracker

    detections = {
        "0": [_det(100, 100)],
        "1": [_det(115, 100)],
        "3": [_det(145, 100)],
    }
    results = IoUTracker(iou_threshold=0.2, max_lost_frames=5).track(detections)
    assert len(results) == 2


# ── Camera-motion compensation ────────────────────────────────────────────────


def test_warp_bbox_identity_when_warp_none() -> None:
    assert warp_bbox([0, 0, 10, 10], None) == [0, 0, 10, 10]


def test_warp_bbox_applies_translation() -> None:
    shift = np.array([[1.0, 0.0, 5.0], [0.0, 1.0, -3.0]])
    assert warp_bbox([0, 0, 10, 10], shift) == [5.0, -3.0, 15.0, 7.0]


def test_camera_motion_compensation_holds_track_through_pan() -> None:
    # The player is static in world space but the camera pans +40 px/frame, so
    # the player's pixel box jumps. ECC warps (per-frame +40 px) cancel the pan.
    detections = {str(f): [_det(100 + 40 * f, 100)] for f in range(4)}
    warps = {f: np.array([[1.0, 0.0, 40.0], [0.0, 1.0, 0.0]]) for f in range(1, 4)}
    results = BoTSORTTracker(
        iou_threshold=0.3, max_lost_frames=5, camera_motion=warps
    ).track(detections)
    assert len(results) == 1
    assert len(results[0].points) == 4


# ── Appearance ────────────────────────────────────────────────────────────────


def test_appearance_breaks_tie_between_crossing_players() -> None:
    # Two players swap pixel positions between frames; identical motion makes IoU
    # ambiguous, but distinct embeddings keep identities stable.
    a0 = _det(100, 100, embedding=[1.0, 0.0])
    b0 = _det(140, 100, embedding=[0.0, 1.0])
    # frame 1: boxes nearly overlap region; correct match is by appearance.
    a1 = _det(118, 100, embedding=[1.0, 0.0])
    b1 = _det(122, 100, embedding=[0.0, 1.0])
    detections = {"0": [a0, b0], "1": [a1, b1]}
    results = BoTSORTTracker(
        iou_threshold=0.05, appearance_weight=0.8, max_lost_frames=5
    ).track(detections)
    # Two stable identities, each two frames long.
    assert len(results) == 2
    assert all(len(t.points) == 2 for t in results)


def test_degrades_to_motion_only_without_embeddings() -> None:
    # appearance_weight set but no embeddings present -> falls back to IoU only.
    detections = {"0": [_det(100, 100)], "1": [_det(101, 100)]}
    results = BoTSORTTracker(appearance_weight=0.9).track(detections)
    assert len(results) == 1


def test_appearance_fn_seeds_track_embeddings() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(101, 100)]}
    tracker = BoTSORTTracker(
        appearance_weight=0.8,
        appearance_fn=lambda _det: np.array([1.0, 0.0]),
    )
    results = tracker.track(detections)
    assert len(results) == 1


def test_empty_input_returns_no_tracks() -> None:
    assert BoTSORTTracker().track({}) == []
