"""roboflow_ops unit tests — pure logic only (no network, no SDK import)."""

from __future__ import annotations

import pytest

from roboflow_ops.frames import frame_window, hamming, is_duplicate, sample_stride
from roboflow_ops.taxonomy import (
    CANONICAL_CLASSES,
    remap_class,
    remap_coco,
)


def test_canonical_classes_match_pipeline_contract() -> None:
    # stage_detect emits exactly these class names; the taxonomy must never
    # drift from them.
    assert CANONICAL_CLASSES == ("player", "official", "ball")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("football-players", "player"),
        ("american-football-players", "player"),
        ("player-white", "player"),
        ("Player-Color", "player"),
        ("db", "player"),
        ("QB", "player"),
        ("SKILL", "player"),
        ("referee", "official"),
        ("ref", "official"),
        ("ball", "ball"),
        ("balllls", "ball"),
        ("ball-possessed", "ball"),
        ("american football", "ball"),
        # Deliberate drops — field furniture and ambiguous labels.
        ("down-indicator", None),
        ("line-to-gain-indicator", None),
        ("whitehat", None),
        ("3", None),
    ],
)
def test_remap_class(source: str, expected: str | None) -> None:
    assert remap_class(source) == expected


def _coco() -> dict:
    return {
        "images": [
            {"id": 1, "file_name": "a.jpg", "width": 100, "height": 100},
            {"id": 2, "file_name": "b.jpg", "width": 100, "height": 100},
        ],
        "categories": [
            {"id": 10, "name": "player-white"},
            {"id": 11, "name": "referee"},
            {"id": 12, "name": "down-indicator"},
            {"id": 13, "name": "ball-grounded"},
        ],
        "annotations": [
            {"id": 1, "image_id": 1, "category_id": 10, "bbox": [1, 1, 5, 5]},
            {"id": 2, "image_id": 1, "category_id": 11, "bbox": [2, 2, 5, 5]},
            {"id": 3, "image_id": 2, "category_id": 12, "bbox": [3, 3, 5, 5]},
            {"id": 4, "image_id": 2, "category_id": 13, "bbox": [4, 4, 5, 5]},
        ],
    }


def test_remap_coco_rewrites_categories_and_drops_unmapped() -> None:
    out, stats = remap_coco(_coco())

    assert [c["name"] for c in out["categories"]] == list(CANONICAL_CLASSES)
    # player id 1, official id 2, ball id 3 (canonical order).
    by_id = {c["name"]: c["id"] for c in out["categories"]}
    kept = {(a["id"], a["category_id"]) for a in out["annotations"]}
    assert kept == {(1, by_id["player"]), (2, by_id["official"]), (4, by_id["ball"])}

    assert stats.kept == {"player": 1, "official": 1, "ball": 1}
    assert stats.dropped == {"down-indicator": 1}
    # Images survive even when their annotations drop (negatives are useful).
    assert len(out["images"]) == 2

    table = stats.table()
    assert "player" in table and "down-indicator" in table


def test_remap_coco_leaves_input_untouched() -> None:
    original = _coco()
    remap_coco(original)
    assert {c["name"] for c in original["categories"]} == {
        "player-white",
        "referee",
        "down-indicator",
        "ball-grounded",
    }
    assert len(original["annotations"]) == 4


def test_sample_stride() -> None:
    assert sample_stride(300, 12) == 25
    assert sample_stride(10, 12) == 1  # short clip: every frame
    assert sample_stride(300, 0) == 300  # non-positive target: single sample


def test_hamming_and_duplicate_detection() -> None:
    assert hamming(0b1010, 0b1010) == 0
    assert hamming(0b1010, 0b0101) == 4
    kept = [0b11110000]
    assert is_duplicate(0b11110001, kept)  # distance 1 ≤ 4 → duplicate
    assert not is_duplicate(0b00001111, kept)  # distance 8 → novel


def test_frame_window_defaults_to_full_video() -> None:
    assert frame_window(30.0, 300, None, None) == (0, 299)


def test_frame_window_clips_to_the_play_interval() -> None:
    # An 8-second play starting at 61.5s in a long recording.
    assert frame_window(30.0, 100_000, 61.5, 69.5) == (1845, 2085)


def test_frame_window_clamps_to_video_bounds() -> None:
    assert frame_window(30.0, 100, 2.0, 999.0) == (60, 99)
    assert frame_window(30.0, 100, 999.0, 9999.0) == (99, 99)
    assert frame_window(30.0, 0, None, None) == (0, 0)
