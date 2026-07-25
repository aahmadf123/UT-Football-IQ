"""Tests for pipeline/detection/dual_res_merger.py (Issue #128)."""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.detection.dual_res_merger import (
    DualResolutionDetector,
    iou,
    merge_dual_resolution,
    nms,
)
from pipeline.detector_models import DetectorBase

# ── IoU ───────────────────────────────────────────────────────────────────────


def test_iou_identical_boxes_is_one() -> None:
    assert iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0


def test_iou_disjoint_boxes_is_zero() -> None:
    assert iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0


def test_iou_half_overlap() -> None:
    # Two unit-area boxes overlapping in half → inter 0.5, union 1.5 → 1/3.
    assert iou([0, 0, 1, 1], [0.5, 0, 1.5, 1]) == 0.5 / 1.5


def test_iou_degenerate_box_is_zero() -> None:
    assert iou([0, 0, 0, 0], [0, 0, 10, 10]) == 0.0


# ── NMS ─────────────────────────────────────────────────────────────────────


def _det(bbox: list[float], conf: float, cls: str = "player") -> dict[str, Any]:
    return {"bbox": bbox, "confidence": conf, "class": cls}


def test_nms_keeps_single_detection() -> None:
    dets = [_det([0, 0, 10, 10], 0.9)]
    assert nms(dets) == dets


def test_nms_suppresses_overlapping_lower_confidence() -> None:
    dets = [_det([0, 0, 10, 10], 0.9), _det([0, 0, 10, 10], 0.5)]
    out = nms(dets, iou_threshold=0.5)
    assert len(out) == 1
    assert out[0]["confidence"] == 0.9


def test_nms_keeps_non_overlapping() -> None:
    dets = [_det([0, 0, 10, 10], 0.9), _det([100, 100, 110, 110], 0.5)]
    assert len(nms(dets, iou_threshold=0.5)) == 2


def test_nms_class_aware_does_not_merge_ball_on_player() -> None:
    # A ball box sitting on top of a player must survive class-aware NMS.
    dets = [_det([0, 0, 10, 10], 0.9, "player"), _det([0, 0, 10, 10], 0.8, "ball")]
    out = nms(dets, iou_threshold=0.5, class_aware=True)
    assert len(out) == 2


def test_nms_class_unaware_merges_across_classes() -> None:
    dets = [_det([0, 0, 10, 10], 0.9, "player"), _det([0, 0, 10, 10], 0.8, "ball")]
    out = nms(dets, iou_threshold=0.5, class_aware=False)
    assert len(out) == 1


# ── merge_dual_resolution ─────────────────────────────────────────────────────


def test_merge_rescales_downscaled_boxes() -> None:
    native: list[dict[str, Any]] = []
    downscaled = [_det([5, 5, 10, 10], 0.6)]
    merged = merge_dual_resolution(native, downscaled, scale=0.5)
    assert len(merged) == 1
    # Boxes rescaled by 1/0.5 = 2×.
    assert merged[0]["bbox"] == [10, 10, 20, 20]


def test_merge_deduplicates_same_object_across_scales() -> None:
    native = [_det([10, 10, 20, 20], 0.9)]
    # Same object found in the downscaled pass (half coords).
    downscaled = [_det([5, 5, 10, 10], 0.6)]
    merged = merge_dual_resolution(native, downscaled, scale=0.5, iou_threshold=0.5)
    assert len(merged) == 1
    assert merged[0]["confidence"] == 0.9  # native (higher conf) wins


def test_merge_keeps_distinct_small_object_from_downscaled() -> None:
    native = [_det([10, 10, 20, 20], 0.9)]
    downscaled = [_det([100, 100, 105, 105], 0.5)]  # → [200,200,210,210]
    merged = merge_dual_resolution(native, downscaled, scale=0.5, iou_threshold=0.5)
    assert len(merged) == 2


# ── DualResolutionDetector adapter ────────────────────────────────────────────


class _CenterBoxDetector(DetectorBase):
    """Returns one box covering the central 20% of whatever frame it sees, so
    the native and downscaled passes describe the same object after rescale."""

    produces_masks = False

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        h, w = frame.shape[:2]
        return [
            {
                "bbox": [w * 0.4, h * 0.4, w * 0.6, h * 0.6],
                "confidence": 0.8,
                "class": "player",
            }
        ]


def test_dual_resolution_adapter_merges_to_single_box() -> None:
    det = DualResolutionDetector(_CenterBoxDetector(), downscale=0.5)
    frame = np.zeros((400, 600, 3), dtype=np.uint8)
    out = det.detect(frame)
    # Native + downscaled describe the same central object → merged to one.
    assert len(out) == 1


def test_dual_resolution_adapter_mirrors_inner_mask_flag() -> None:
    det = DualResolutionDetector(_CenterBoxDetector(), downscale=0.5)
    assert det.produces_masks is False
