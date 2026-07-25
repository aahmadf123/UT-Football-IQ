"""Tests for pipeline/detection/official_suppressor.py (Issue #148)."""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.detection.official_suppressor import (
    OfficialSuppressor,
    bw_bimodality_score,
    filter_non_players,
    is_non_player,
    official_score,
    vertical_stripe_score,
)

# ── Synthetic crop / frame builders ───────────────────────────────────────────


def _striped_crop(h: int = 40, w: int = 100, block: int = 6) -> np.ndarray:
    """Vertical black/white stripes — the referee-shirt signature."""
    crop = np.zeros((h, w, 3), dtype=np.uint8)
    for col in range(w):
        if (col // block) % 2 == 0:
            crop[:, col, :] = 255
    return crop


def _solid_crop(h: int = 40, w: int = 100, color: tuple[int, int, int] = (50, 50, 200)) -> np.ndarray:
    crop = np.zeros((h, w, 3), dtype=np.uint8)
    crop[:, :] = color
    return crop


def _frame_with_torso(
    bbox: list[float], torso: np.ndarray, shape: tuple[int, int] = (200, 200)
) -> np.ndarray:
    """Place ``torso`` into the torso band of ``bbox`` on a black frame."""
    frame = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    x1, y1, x2, y2 = (int(v) for v in bbox)
    bh = y2 - y1
    ty1 = y1 + int(0.15 * bh)
    ty2 = y1 + int(0.55 * bh)
    region = frame[ty1:ty2, x1:x2]
    th, tw = region.shape[:2]
    frame[ty1:ty2, x1:x2] = torso[:th, :tw]
    return frame


# ── Stripe / bimodality scores ────────────────────────────────────────────────


def test_vertical_stripe_score_high_for_stripes() -> None:
    assert vertical_stripe_score(_striped_crop()) > 0.6


def test_vertical_stripe_score_low_for_solid() -> None:
    assert vertical_stripe_score(_solid_crop()) < 0.2


def test_vertical_stripe_score_empty_is_zero() -> None:
    assert vertical_stripe_score(np.empty((0, 0, 3), np.uint8)) == 0.0


def test_bw_bimodality_high_for_black_white() -> None:
    assert bw_bimodality_score(_striped_crop()) > 0.9


def test_bw_bimodality_low_for_colored_jersey() -> None:
    assert bw_bimodality_score(_solid_crop()) < 0.2


def test_official_score_separates_official_from_player() -> None:
    assert official_score(_striped_crop()) >= 0.6
    assert official_score(_solid_crop()) < 0.3


# ── OfficialSuppressor.suppress ───────────────────────────────────────────────


def _det(bbox: list[float], cls: str = "player") -> dict[str, Any]:
    return {"bbox": bbox, "confidence": 0.8, "class": cls}


def test_suppress_relabels_striped_official() -> None:
    bbox = [40.0, 40.0, 140.0, 140.0]
    frame = _frame_with_torso(bbox, _striped_crop())
    out = OfficialSuppressor().suppress(frame, [_det(bbox)])
    assert len(out) == 1  # nothing deleted
    assert out[0]["class"] == "official"


def test_suppress_keeps_real_player() -> None:
    bbox = [40.0, 40.0, 140.0, 140.0]
    frame = _frame_with_torso(bbox, _solid_crop())
    out = OfficialSuppressor().suppress(frame, [_det(bbox)])
    assert out[0]["class"] == "player"


def test_suppress_never_touches_ball() -> None:
    bbox = [40.0, 40.0, 140.0, 140.0]
    frame = _frame_with_torso(bbox, _striped_crop())
    out = OfficialSuppressor().suppress(frame, [_det(bbox, cls="ball")])
    assert out[0]["class"] == "ball"


def test_suppress_disabled_is_passthrough() -> None:
    bbox = [40.0, 40.0, 140.0, 140.0]
    frame = _frame_with_torso(bbox, _striped_crop())
    dets = [_det(bbox)]
    out = OfficialSuppressor(enabled=False).suppress(frame, dets)
    assert out is dets
    assert out[0]["class"] == "player"


def test_suppress_high_threshold_keeps_player_label() -> None:
    # A conservative (impossible) threshold never relabels — the failure mode
    # that protects valid players.
    bbox = [40.0, 40.0, 140.0, 140.0]
    frame = _frame_with_torso(bbox, _striped_crop())
    out = OfficialSuppressor(threshold=1.1).suppress(frame, [_det(bbox)])
    assert out[0]["class"] == "player"


def test_suppress_does_not_mutate_input_detection() -> None:
    bbox = [40.0, 40.0, 140.0, 140.0]
    frame = _frame_with_torso(bbox, _striped_crop())
    original = _det(bbox)
    OfficialSuppressor().suppress(frame, [original])
    assert original["class"] == "player"  # copy-on-relabel


# ── Sideline tagging via field-of-play polygon ────────────────────────────────


def test_off_field_player_tagged_sideline() -> None:
    field = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
    bbox = [150.0, 150.0, 170.0, 180.0]  # foot point (160, 180) is off-field
    frame = _frame_with_torso(bbox, _solid_crop(), shape=(220, 220))
    sup = OfficialSuppressor(field_of_play=field)
    out = sup.suppress(frame, [_det(bbox)])
    assert out[0]["class"] == "sideline"


def test_in_field_player_stays_player_with_polygon() -> None:
    field = [[0.0, 0.0], [200.0, 0.0], [200.0, 200.0], [0.0, 200.0]]
    bbox = [40.0, 40.0, 140.0, 140.0]  # foot point in field
    frame = _frame_with_torso(bbox, _solid_crop())
    sup = OfficialSuppressor(field_of_play=field)
    out = sup.suppress(frame, [_det(bbox)])
    assert out[0]["class"] == "player"


# ── filter_non_players (used by stage_labels) ─────────────────────────────────


def test_is_non_player_reads_class_and_role() -> None:
    assert is_non_player({"class": "official"}) is True
    assert is_non_player({"class": "sideline"}) is True
    assert is_non_player({"role": "official"}) is True
    assert is_non_player({"class": "player"}) is False


def test_filter_non_players_keeps_unlabeled_as_players() -> None:
    items = [
        {"id": "a", "class": "player"},
        {"id": "b", "class": "official"},
        {"id": "c", "class": "sideline"},
        {"id": "d"},  # no class → treated as player (backward compatible)
    ]
    kept = filter_non_players(items)
    assert [it["id"] for it in kept] == ["a", "d"]
