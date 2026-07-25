"""Tests for pipeline/detection/sahi_wrapper.py (Issues #128 / #133)."""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.detection.sahi_wrapper import (
    BALL_TILE,
    PLAYER_TILE,
    SAHIDetectorAdapter,
    slice_offsets,
)
from pipeline.detector_models import DetectorBase

# ── slice_offsets geometry ────────────────────────────────────────────────────


def test_slice_offsets_small_frame_single_window() -> None:
    # Frame smaller than a tile → one full-frame window.
    assert slice_offsets(300, 200, tile=400, overlap=0.2) == [(0, 0, 300, 200)]


def test_slice_offsets_covers_whole_frame_to_edges() -> None:
    windows = slice_offsets(1000, 600, tile=400, overlap=0.2)
    assert windows  # non-empty
    # The union of windows must reach the far edges.
    assert max(w[2] for w in windows) == 1000
    assert max(w[3] for w in windows) == 600
    # No window runs off the frame.
    for x1, y1, x2, y2 in windows:
        assert 0 <= x1 < x2 <= 1000
        assert 0 <= y1 < y2 <= 600


def test_slice_offsets_overlap_produces_multiple_tiles() -> None:
    windows = slice_offsets(1000, 1000, tile=400, overlap=0.2)
    assert len(windows) >= 4  # a 1000² frame needs a grid of ≥ 2×2 tiles


# ── SAHI adapter ──────────────────────────────────────────────────────────────


class _TileLocalDetector(DetectorBase):
    """Emits one fixed box near the top-left of every tile it's handed, so we
    can verify per-tile offsetting back to frame coordinates."""

    produces_masks = False

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        return [{"bbox": [5.0, 5.0, 15.0, 15.0], "confidence": 0.7, "class": "player"}]


def test_sahi_offsets_detections_into_frame_coords() -> None:
    inner = _TileLocalDetector()
    adapter = SAHIDetectorAdapter(
        inner, tile=PLAYER_TILE, overlap=0.2, full_frame_pass=False
    )
    frame = np.zeros((1000, 1000, 3), dtype=np.uint8)
    dets = adapter.detect(frame)
    windows = slice_offsets(1000, 1000, tile=PLAYER_TILE, overlap=0.2)
    # One small, non-overlapping box per tile survives NMS.
    assert len(dets) == len(windows)
    # Every box sits within the frame.
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        assert 0 <= x1 < x2 <= 1000
        assert 0 <= y1 < y2 <= 1000
    # At least one box is offset away from the origin (a non-(0,0) tile).
    assert any(d["bbox"][0] > 15.0 for d in dets)


class _CountingDetector(DetectorBase):
    """Counts how many times ``detect`` is invoked (one call per inference)."""

    produces_masks = False

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        self.calls += 1
        return []


def test_sahi_full_frame_pass_runs_inner_one_extra_time() -> None:
    frame = np.zeros((1000, 1000, 3), dtype=np.uint8)
    n_tiles = len(slice_offsets(1000, 1000, tile=PLAYER_TILE, overlap=0.2))

    without = _CountingDetector()
    SAHIDetectorAdapter(without, tile=PLAYER_TILE, full_frame_pass=False).detect(frame)
    assert without.calls == n_tiles

    with_full = _CountingDetector()
    SAHIDetectorAdapter(with_full, tile=PLAYER_TILE, full_frame_pass=True).detect(frame)
    assert with_full.calls == n_tiles + 1


def test_sahi_small_frame_runs_inner_once() -> None:
    inner = _TileLocalDetector()
    adapter = SAHIDetectorAdapter(
        inner, tile=PLAYER_TILE, overlap=0.2, full_frame_pass=False
    )
    frame = np.zeros((200, 200, 3), dtype=np.uint8)  # smaller than tile
    dets = adapter.detect(frame)
    assert len(dets) == 1


def test_sahi_ball_preset_uses_smaller_tiles() -> None:
    assert BALL_TILE < PLAYER_TILE
    inner = _TileLocalDetector()
    adapter = SAHIDetectorAdapter(inner, tile=BALL_TILE, overlap=0.2, full_frame_pass=False)
    frame = np.zeros((512, 512, 3), dtype=np.uint8)
    windows = slice_offsets(512, 512, tile=BALL_TILE, overlap=0.2)
    assert len(adapter.detect(frame)) == len(windows)


def test_sahi_mirrors_inner_mask_flag() -> None:
    adapter = SAHIDetectorAdapter(_TileLocalDetector(), tile=PLAYER_TILE)
    assert adapter.produces_masks is False
