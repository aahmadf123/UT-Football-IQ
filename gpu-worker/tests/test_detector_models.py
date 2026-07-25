"""Tests for gpu-worker/pipeline/detector_models.py (Issue #74).

Covers the detector adapter pattern: every adapter returns the same
detection schema, the optional ``mask`` field is opt-in, and the factory
falls back to the stub when ultralytics / SAM 3.1 are unavailable.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.detector_models import (
    COCO_CLASS_NAMES,
    MASK_FORMAT_POLYGON,
    DetectorBase,
    SAM3Detector,
    StubDetector,
    YOLODetector,
    detection_has_mask,
    get_detector,
    polygon_area,
)

# ── Schema invariants ─────────────────────────────────────────────────────────


def _assert_detection_schema(det: dict[str, Any]) -> None:
    assert "bbox" in det and isinstance(det["bbox"], list)
    assert len(det["bbox"]) == 4
    for v in det["bbox"]:
        assert isinstance(v, float)
    assert "confidence" in det and isinstance(det["confidence"], float)
    assert 0.0 <= det["confidence"] <= 1.0
    assert "class" in det and isinstance(det["class"], str)
    if "mask" in det:
        assert det["mask"]["format"] == MASK_FORMAT_POLYGON
        assert isinstance(det["mask"]["points"], list)
        assert len(det["mask"]["points"]) >= 3


def test_stub_detector_returns_valid_schema() -> None:
    detector = StubDetector(n_players=3)
    dets = detector.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
    assert len(dets) == 3
    for d in dets:
        _assert_detection_schema(d)
        assert "mask" not in d  # mask off by default


def test_stub_detector_with_masks_adds_mask_field() -> None:
    detector = StubDetector(n_players=2, produces_masks=True)
    dets = detector.detect(np.zeros((720, 1280, 3), dtype=np.uint8))
    assert len(dets) == 2
    for d in dets:
        _assert_detection_schema(d)
        assert "mask" in d
        assert d["mask"]["format"] == MASK_FORMAT_POLYGON
        assert len(d["mask"]["points"]) == 4


def test_stub_detector_deterministic_across_calls() -> None:
    d1 = StubDetector(n_players=4, seed=7).detect(np.zeros((10, 10, 3), np.uint8))
    d2 = StubDetector(n_players=4, seed=7).detect(np.zeros((10, 10, 3), np.uint8))
    assert d1 == d2


def test_stub_detector_returns_independent_copies() -> None:
    detector = StubDetector(n_players=2, produces_masks=True)
    a = detector.detect(np.zeros((10, 10, 3), np.uint8))
    a[0]["bbox"][0] = -999.0
    a[0]["mask"]["points"][0][0] = -999.0
    b = detector.detect(np.zeros((10, 10, 3), np.uint8))
    assert b[0]["bbox"][0] != -999.0
    assert b[0]["mask"]["points"][0][0] != -999.0


def test_stub_detector_produces_masks_flag_matches_output() -> None:
    bbox_only = StubDetector(produces_masks=False)
    masked = StubDetector(produces_masks=True)
    assert bbox_only.produces_masks is False
    assert masked.produces_masks is True


# ── Factory behaviour ─────────────────────────────────────────────────────────


def test_get_detector_unknown_variant_falls_back_to_stub() -> None:
    det = get_detector("does-not-exist")
    assert isinstance(det, StubDetector)


def test_get_detector_stub_returns_stub() -> None:
    det = get_detector("stub")
    assert isinstance(det, StubDetector)


def test_get_detector_yolo_falls_back_when_ultralytics_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the YOLODetector constructor to raise ImportError so we can
    # exercise the fallback path without uninstalling ultralytics.
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ImportError("ultralytics missing for test")

    monkeypatch.setattr(
        "pipeline.detector_models.YOLODetector.__init__", _boom
    )
    det = get_detector("yolov8n")
    assert isinstance(det, StubDetector)
    assert det.produces_masks is False


def test_get_detector_sam3_falls_back_to_stub_with_masks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ImportError("ultralytics SAM missing for test")

    monkeypatch.setattr(
        "pipeline.detector_models.SAM3Detector.__init__", _boom
    )
    det = get_detector("sam3.1")
    assert isinstance(det, StubDetector)
    # SAM 3 fallback still produces masks so downstream mask consumers
    # can exercise their happy path in tests.
    assert det.produces_masks is True


def test_get_detector_empty_string_returns_stub() -> None:
    det = get_detector("")
    assert isinstance(det, StubDetector)


# ── Helpers ───────────────────────────────────────────────────────────────────


def test_detection_has_mask_true_for_polygon() -> None:
    det = {"bbox": [0, 0, 1, 1], "confidence": 0.5, "class": "player",
           "mask": {"format": "polygon", "points": [[0, 0], [1, 0], [0, 1]]}}
    assert detection_has_mask(det) is True


def test_detection_has_mask_false_when_missing() -> None:
    det = {"bbox": [0, 0, 1, 1], "confidence": 0.5, "class": "player"}
    assert detection_has_mask(det) is False


def test_detection_has_mask_false_for_degenerate_polygon() -> None:
    det = {"bbox": [0, 0, 1, 1], "confidence": 0.5, "class": "player",
           "mask": {"format": "polygon", "points": [[0, 0]]}}
    assert detection_has_mask(det) is False


def test_polygon_area_unit_square() -> None:
    assert polygon_area([[0, 0], [1, 0], [1, 1], [0, 1]]) == pytest.approx(1.0)


def test_polygon_area_zero_for_degenerate() -> None:
    assert polygon_area([[0, 0], [1, 1]]) == 0.0
    assert polygon_area([]) == 0.0


def test_polygon_area_clockwise_and_ccw_match() -> None:
    cw = [[0, 0], [0, 1], [1, 1], [1, 0]]
    ccw = [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert polygon_area(cw) == pytest.approx(polygon_area(ccw))


# ── Class map / COCO sanity ───────────────────────────────────────────────────


def test_coco_class_names_contain_player_and_ball() -> None:
    assert COCO_CLASS_NAMES[0] == "player"
    assert COCO_CLASS_NAMES[32] == "ball"


# ── Adapter base contract ─────────────────────────────────────────────────────


def test_detector_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        DetectorBase()  # type: ignore[abstract]


def test_yolo_and_sam3_class_attributes_match_expected_mask_flag() -> None:
    # Class-level annotations should reflect mask-production capability
    # before any instance is constructed, so the model_router can
    # consult them ahead of time.
    assert YOLODetector.produces_masks is False
    assert SAM3Detector.produces_masks is True
