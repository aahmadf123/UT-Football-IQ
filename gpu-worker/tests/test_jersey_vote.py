"""Jersey-number voting and shared numbers (stage_reid)."""

import os
import sys
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import stage_reid
from pipeline.stage_reid import jersey_vote
from pipeline.tracking.easyocr_jersey import EasyOCRJerseyReader


def test_easyocr_rejects_split_digit_detections() -> None:
    class SplitDigitReader:
        def readtext(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return [
                ([], "7", 0.95),
                ([], "5", 0.90),
            ]

    reader = EasyOCRJerseyReader(SplitDigitReader())
    assert reader.recognize(np.zeros((96, 96, 3), dtype=np.uint8)) == (None, 0.0)


def test_vote_needs_agreeing_sightings() -> None:
    assert jersey_vote({50: 3}) == 50
    assert jersey_vote({50: 2}) is None  # too few sightings
    assert jersey_vote({7: 3, 75: 2}) is None  # partial reads disagree
    assert jersey_vote({12: 4, 2: 1}) == 12
    assert jersey_vote({}) is None


def test_shared_numbers_are_never_assigned_by_number_alone() -> None:
    roster = [
        {"id": "qb7", "jersey_number": 7, "position": "QB"},
        {"id": "s7", "jersey_number": 7, "position": "S"},
        {"id": "ol50", "jersey_number": 50, "position": "OL"},
    ]
    seen: dict[str, object] = {}

    def fake_identify(tracklet, cap, jersey_map, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen["map"] = dict(jersey_map)
        return None

    with (
        patch.object(stage_reid, "_identify_tracklet", fake_identify),
        patch.object(stage_reid, "_get_reid_adapter", lambda: None),
        patch.object(stage_reid, "_get_ocr_adapter", lambda _v: None),
        patch.object(stage_reid.cv2, "VideoCapture"),
        patch.object(stage_reid.VideoTimeline, "from_path"),
    ):
        stage_reid.run(
            "clip",
            "film.mp4",
            ["t1"],
            [{"id": "t1", "track_points": [{"frame_number": 0, "bbox": [0, 0, 10, 20]}]}],
            roster,
            "",
        )
    assert seen["map"] == {50: "ol50"}
