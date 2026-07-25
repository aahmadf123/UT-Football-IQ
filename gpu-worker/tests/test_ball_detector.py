"""Tests for the dedicated ball detector + manifest schema (Issue #133)."""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.ball import MANIFEST_VERSION, validate_manifest
from pipeline.detection.ball_detector import (
    YOLO_BALL,
    BallDetectorAdapter,
    _StubBallDetector,
    ball_strategy,
    build_ball_detector,
    get_ball_detector,
)
from pipeline.detection.sahi_wrapper import BALL_TILE, SAHIDetectorAdapter
from pipeline.detector_models import DetectorBase

# ── BallDetectorAdapter ───────────────────────────────────────────────────────


class _MultiClassDetector(DetectorBase):
    produces_masks = False

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        return [
            {"bbox": [1, 2, 3, 4], "confidence": 0.9, "class": "thing-0"},
            {"bbox": [5, 6, 7, 8], "confidence": 0.4, "class": "anything"},
        ]


def test_ball_adapter_normalises_class_to_ball() -> None:
    adapter = BallDetectorAdapter(_MultiClassDetector())
    dets = adapter.detect(np.zeros((100, 100, 3), np.uint8))
    assert dets  # adapter keeps the inner detections
    for d in dets:
        assert d["class"] == "ball"
        assert isinstance(d["confidence"], float)
        assert len(d["bbox"]) == 4


def test_ball_adapter_produces_no_masks() -> None:
    assert BallDetectorAdapter(_MultiClassDetector()).produces_masks is False


def test_stub_ball_detector_returns_one_ball() -> None:
    dets = _StubBallDetector().detect(np.zeros((720, 1280, 3), np.uint8))
    assert len(dets) == 1
    assert dets[0]["class"] == "ball"


# ── Factory + regime composition ──────────────────────────────────────────────


def test_get_ball_detector_falls_back_to_stub_without_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ImportError("ultralytics missing for test")

    monkeypatch.setattr("pipeline.detector_models.YOLODetector.__init__", _boom)
    det = get_ball_detector(YOLO_BALL)
    assert isinstance(det, _StubBallDetector)


def test_get_ball_detector_unknown_variant_uses_stub() -> None:
    assert isinstance(get_ball_detector("not-a-ball-model"), _StubBallDetector)


def test_build_ball_detector_drone_follow_wraps_sahi() -> None:
    base = _StubBallDetector()
    det = build_ball_detector(YOLO_BALL, "drone_follow", base=base)
    assert isinstance(det, SAHIDetectorAdapter)


def test_build_ball_detector_fixed_sideline_is_full_frame() -> None:
    base = _StubBallDetector()
    det = build_ball_detector(YOLO_BALL, "fixed_sideline", base=base)
    assert det is base  # no SAHI wrapping on fixed sideline


def test_build_ball_detector_unknown_regime_is_full_frame() -> None:
    base = _StubBallDetector()
    assert build_ball_detector(YOLO_BALL, None, base=base) is base


def test_ball_strategy_labels() -> None:
    assert ball_strategy("drone_follow") == f"sahi-{BALL_TILE}"
    assert ball_strategy("fixed_sideline") == "full-frame"
    assert ball_strategy(None) == "full-frame"


# ── Annotation manifest schema ────────────────────────────────────────────────


def test_validate_manifest_accepts_well_formed() -> None:
    manifest = {
        "version": MANIFEST_VERSION,
        "annotations": [
            {
                "video_id": "abc",
                "frame_number": 10,
                "bbox": [1.0, 2.0, 3.0, 4.0],
                "visible": True,
                "regime": "drone_follow",
                "split": "train",
            }
        ],
    }
    assert validate_manifest(manifest) == []


def test_validate_manifest_rejects_bad_version() -> None:
    errors = validate_manifest({"version": 999, "annotations": []})
    assert any("version" in e for e in errors)


def test_validate_manifest_rejects_bad_regime_and_split() -> None:
    manifest = {
        "version": MANIFEST_VERSION,
        "annotations": [
            {
                "video_id": "abc",
                "frame_number": 10,
                "bbox": [1, 2, 3, 4],
                "regime": "soccer-pitch",
                "split": "holdout",
            }
        ],
    }
    errors = validate_manifest(manifest)
    assert any("regime" in e for e in errors)
    assert any("split" in e for e in errors)


def test_validate_manifest_requires_annotations_list() -> None:
    assert any("annotations" in e for e in validate_manifest({"version": MANIFEST_VERSION}))
