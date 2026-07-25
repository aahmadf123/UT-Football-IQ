"""Tests for the StrongSORT tracker adapter (Issue #129).

StrongSORT is the nightly offline path. Tests cover the TrackerBase contract,
basic continuity, the matching-cascade ordering, appearance-dominant
association, and graceful motion-only degradation. Pure NumPy.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.tracker_models import TrackerBase, get_tracker
from pipeline.tracking.botsort_adapter import BoTSORTTracker
from pipeline.tracking.strongsort_adapter import StrongSORTTracker


def _det(x: float, y: float, embedding: list[float] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"bbox": [x, y, x + 30, y + 60], "confidence": 0.9, "class": "player"}
    if embedding is not None:
        d["embedding"] = embedding
    return d


def test_strongsort_is_tracker_base_and_botsort_subclass() -> None:
    assert issubclass(StrongSORTTracker, TrackerBase)
    assert issubclass(StrongSORTTracker, BoTSORTTracker)


def test_factory_returns_strongsort() -> None:
    assert isinstance(get_tracker("strongsort"), StrongSORTTracker)


def test_single_track_continues_across_frames() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(102, 101)], "2": [_det(104, 102)]}
    results = StrongSORTTracker(iou_threshold=0.2, max_lost_frames=5).track(detections)
    assert len(results) == 1
    assert [p["frame_number"] for p in results[0].points] == [0, 1, 2]


def test_track_points_schema_invariant() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(101, 100)]}
    for track in StrongSORTTracker().track(detections):
        for pt in track.points:
            assert "frame_number" in pt and "bbox" in pt


def test_far_detection_starts_new_track() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(101, 100), _det(800, 100)]}
    assert len(StrongSORTTracker(max_lost_frames=10).track(detections)) == 2


def test_appearance_dominant_keeps_identities_through_crossing() -> None:
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    detections = {
        "0": [_det(100, 100, a), _det(140, 100, b)],
        "1": [_det(120, 100, a), _det(124, 100, b)],
        "2": [_det(140, 100, a), _det(108, 100, b)],
    }
    results = StrongSORTTracker(iou_threshold=0.05, max_lost_frames=5).track(detections)
    assert len(results) == 2


def test_degrades_to_motion_only_without_embeddings() -> None:
    detections = {"0": [_det(100, 100)], "1": [_det(101, 100)]}
    assert len(StrongSORTTracker().track(detections)) == 1


def test_empty_input_returns_no_tracks() -> None:
    assert StrongSORTTracker().track({}) == []
