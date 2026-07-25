"""Tests for gpu-worker/pipeline/tracker_models.py (Issue #74).

Covers:
  - Schema invariants on the optional ``mask`` propagation
  - IoUTracker numerical parity with the original ``stage_track`` path
  - SAM3MaskTracker behaviour with and without masks (mixed input)
  - Track-point lifecycle: starts on first sight, closes after lost frames
  - Factory dispatching
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.tracker_models import (
    IoUTracker,
    SAM3MaskTracker,
    StubTracker,
    TrackerBase,
    bbox_iou,
    get_tracker,
    mask_overlap_score,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _bbox_det(x: float, y: float) -> dict[str, Any]:
    return {"bbox": [x, y, x + 30, y + 60], "confidence": 0.9, "class": "player"}


def _mask_det(x: float, y: float) -> dict[str, Any]:
    det = _bbox_det(x, y)
    x1, y1, x2, y2 = det["bbox"]
    det["mask"] = {
        "format": "polygon",
        "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
    }
    return det


# ── Geometry helpers ──────────────────────────────────────────────────────────


def test_bbox_iou_identical_boxes_returns_one() -> None:
    assert bbox_iou([0, 0, 10, 10], [0, 0, 10, 10]) == pytest.approx(1.0)


def test_bbox_iou_disjoint_returns_zero() -> None:
    assert bbox_iou([0, 0, 10, 10], [100, 100, 110, 110]) == 0.0


def test_bbox_iou_half_overlap() -> None:
    iou = bbox_iou([0, 0, 10, 10], [5, 0, 15, 10])
    assert iou == pytest.approx(50 / 150)


def test_mask_overlap_score_zero_when_either_missing() -> None:
    poly = {"format": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    assert mask_overlap_score(poly, None) == 0.0
    assert mask_overlap_score(None, poly) == 0.0


def test_mask_overlap_score_unit_squares_match() -> None:
    poly = {"format": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    assert mask_overlap_score(poly, poly) == pytest.approx(1.0)


def test_mask_overlap_score_shrunken_mask_lower_score() -> None:
    big = {"format": "polygon", "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}
    small = {"format": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]}
    assert mask_overlap_score(big, small) == pytest.approx(0.01)


# ── IoU tracker (numerical parity with legacy stage_track) ────────────────────


def test_iou_tracker_single_track_continues_across_frames() -> None:
    detections = {
        "0": [_bbox_det(100, 100)],
        "1": [_bbox_det(102, 101)],
        "2": [_bbox_det(104, 102)],
    }
    results = IoUTracker(iou_threshold=0.3, max_lost_frames=5).track(detections)
    assert len(results) == 1
    track = results[0]
    assert track.start_frame == 0
    assert track.last_frame == 2
    assert [p["frame_number"] for p in track.points] == [0, 1, 2]


def test_iou_tracker_starts_new_track_for_far_detection() -> None:
    detections = {
        "0": [_bbox_det(100, 100)],
        "1": [_bbox_det(102, 101), _bbox_det(800, 100)],
    }
    results = IoUTracker(max_lost_frames=10).track(detections)
    assert len(results) == 2


def test_iou_tracker_closes_track_after_lost_frames() -> None:
    detections: dict[str, list[dict[str, Any]]] = {
        "0": [_bbox_det(100, 100)],
    }
    # Insert empty frames to age out the active track
    for f in range(1, 35):
        detections[str(f)] = []
    detections["50"] = [_bbox_det(500, 500)]  # new far-away track
    results = IoUTracker(max_lost_frames=30).track(detections)
    assert len(results) == 2


def test_iou_tracker_ignores_non_player_detections() -> None:
    detections = {
        "0": [
            _bbox_det(100, 100),
            {"bbox": [50, 50, 60, 60], "confidence": 0.5, "class": "ball"},
        ],
    }
    results = IoUTracker().track(detections)
    assert len(results) == 1
    assert "mask" not in results[0].points[0]


def test_iou_tracker_drops_mask_aware_flag() -> None:
    assert IoUTracker().mask_aware is False


# ── Mask propagation: bbox-only input still works ─────────────────────────────


def test_iou_tracker_propagates_no_mask_when_detector_omits_it() -> None:
    detections = {
        "0": [_bbox_det(100, 100)],
        "1": [_bbox_det(102, 101)],
    }
    results = IoUTracker().track(detections)
    for pt in results[0].points:
        assert "mask" not in pt


def test_sam3_mask_tracker_falls_back_to_iou_when_masks_absent() -> None:
    detections = {
        "0": [_bbox_det(100, 100)],
        "1": [_bbox_det(102, 101)],
        "2": [_bbox_det(104, 102)],
    }
    results = SAM3MaskTracker().track(detections)
    assert len(results) == 1
    for pt in results[0].points:
        assert "mask" not in pt


def test_sam3_mask_tracker_propagates_masks_when_present() -> None:
    detections = {
        "0": [_mask_det(100, 100)],
        "1": [_mask_det(102, 101)],
    }
    results = SAM3MaskTracker().track(detections)
    assert len(results) == 1
    for pt in results[0].points:
        assert "mask" in pt
        assert pt["mask"]["format"] == "polygon"
        assert len(pt["mask"]["points"]) == 4


def test_sam3_mask_tracker_handles_mixed_mask_and_bbox_only() -> None:
    detections = {
        "0": [_mask_det(100, 100)],
        "1": [_bbox_det(102, 101)],  # mask dropped on frame 1
        "2": [_mask_det(104, 102)],
    }
    results = SAM3MaskTracker().track(detections)
    assert len(results) == 1
    masks = ["mask" in p for p in results[0].points]
    assert masks == [True, False, True]


def test_sam3_mask_tracker_is_mask_aware_flag() -> None:
    assert SAM3MaskTracker().mask_aware is True


# ── Stub tracker ──────────────────────────────────────────────────────────────


def test_stub_tracker_creates_one_track_per_detection_per_frame() -> None:
    detections = {
        "0": [_bbox_det(0, 0), _bbox_det(100, 100)],
        "1": [_bbox_det(0, 0), _bbox_det(100, 100)],
    }
    results = StubTracker().track(detections)
    # Stub treats each (frame, slot) as its own track
    assert len(results) >= 2


# ── Factory ───────────────────────────────────────────────────────────────────


def test_get_tracker_iou_returns_iou_tracker() -> None:
    assert isinstance(get_tracker("iou-tracker"), IoUTracker)


def test_get_tracker_sam3_returns_sam3_mask_tracker() -> None:
    assert isinstance(get_tracker("sam3-mask-tracker"), SAM3MaskTracker)


def test_get_tracker_stub_returns_stub() -> None:
    assert isinstance(get_tracker("stub"), StubTracker)


def test_get_tracker_unknown_falls_back_to_iou() -> None:
    assert isinstance(get_tracker("ghost-tracker"), IoUTracker)


def test_get_tracker_empty_string_returns_iou() -> None:
    assert isinstance(get_tracker(""), IoUTracker)


def test_tracker_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        TrackerBase()  # type: ignore[abstract]


# ── Backwards compatibility: schema fields tracker emits ──────────────────────


def test_track_points_always_have_frame_and_bbox() -> None:
    detections = {
        "0": [_mask_det(100, 100)],
        "1": [_mask_det(102, 101)],
    }
    results = SAM3MaskTracker().track(detections)
    for track in results:
        for pt in track.points:
            assert "frame_number" in pt
            assert "bbox" in pt
            assert len(pt["bbox"]) == 4
