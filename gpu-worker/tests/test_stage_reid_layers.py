"""Integration tests for the layered Re-ID stage (Issue #131).

Exercises the trajectory-prior and min-cost-flow layers of ``stage_reid.run``
without a real video or backend: ``cv2.VideoCapture`` is stubbed to yield no
frames (so the OCR layer is a no-op) and ``_patch_tracklet`` is captured. This
covers same-session vs nightly behaviour and the "never overwrite an existing
player_id / no schema change" invariant.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY

from pipeline import stage_reid


class _NoFrameCapture:
    """Stub cv2.VideoCapture that never yields a frame (OCR becomes a no-op)."""

    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def set(self, *_a: Any) -> None:
        pass

    def read(self) -> tuple[bool, None]:
        return False, None

    def release(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _stub_video_and_backend(monkeypatch: pytest.MonkeyPatch):
    patched: list[tuple[str, str]] = []
    monkeypatch.setattr(stage_reid.cv2, "VideoCapture", _NoFrameCapture)
    monkeypatch.setattr(
        stage_reid,
        "_patch_tracklet",
        lambda tid, pid, url: patched.append((tid, pid)),
    )
    # No external ReID/PARSeq adapters in unit tests.
    monkeypatch.setattr(stage_reid, "_get_reid_adapter", lambda: None)
    monkeypatch.setattr(stage_reid, "_get_ocr_adapter", lambda variant: None)
    monkeypatch.setattr(stage_reid, "PATCHED", patched, raising=False)
    return patched


def _points(frames: list[int], xs: list[float], y: float = 100.0) -> list[dict[str, Any]]:
    return [
        {"frame_number": f, "bbox": [x, y, x + 30, y + 60]}
        for f, x in zip(frames, xs)
    ]


def _run(tracklets: list[dict[str, Any]], priority: int) -> dict[str, Any]:
    return stage_reid.run(
        clip_id="clip1",
        video_path=Path("/does/not/exist.mp4"),
        tracklet_ids=[t["id"] for t in tracklets],
        tracklets=tracklets,
        roster=[],
        backend_api_url="",
        variant="parseq-ocr" if priority <= NIGHTLY_PRIORITY else "jersey-ocr",
        priority=priority,
    )


def test_trajectory_prior_assigns_unidentified_track(_stub_video_and_backend) -> None:
    patched = _stub_video_and_backend
    known = {
        "id": "tk-known",
        "player_id": "player-7",
        "team_label": "unknown",
        "track_points": _points([0, 1, 2], [100, 110, 120]),  # vel +10/frame
    }
    unknown = {
        "id": "tk-unknown",
        "player_id": None,
        "team_label": "unknown",
        # Starts at frame 3 near where player-7 is predicted to be (~130).
        "track_points": _points([3, 4], [130, 140]),
    }
    result = _run([known, unknown], SAME_SESSION_PRIORITY)
    assert ("tk-unknown", "player-7") in patched
    assert result["assigned"] >= 1
    # Same-session keeps stitching off.
    assert result["reid_strategy"]["stitching_enabled"] is False


def test_existing_player_id_is_never_overwritten(_stub_video_and_backend) -> None:
    patched = _stub_video_and_backend
    a = {
        "id": "tk-a",
        "player_id": "player-1",
        "team_label": "unknown",
        "track_points": _points([0, 1], [100, 105]),
    }
    b = {
        "id": "tk-b",
        "player_id": "player-2",
        "team_label": "unknown",
        "track_points": _points([0, 1], [400, 405]),
    }
    _run([a, b], NIGHTLY_PRIORITY)
    # Neither already-identified tracklet should be re-patched.
    assert all(tid not in ("tk-a", "tk-b") for tid, _ in patched)


def test_nightly_stitching_propagates_player_id(_stub_video_and_backend) -> None:
    patched = _stub_video_and_backend
    # A fragmented identity: head is known, tail is an unidentified continuation
    # that is too far in time for OCR/gallery but stitchable by motion.
    head = {
        "id": "tk-head",
        "player_id": "player-9",
        "team_label": "unknown",
        "track_points": _points(list(range(0, 10)), [i * 5 for i in range(10)]),
    }
    tail = {
        "id": "tk-tail",
        "player_id": None,
        "team_label": "unknown",
        "track_points": _points(list(range(12, 20)), [60 + (i - 12) * 5 for i in range(12, 20)]),
    }
    result = _run([head, tail], NIGHTLY_PRIORITY)
    assert ("tk-tail", "player-9") in patched
    assert result["reid_strategy"]["stitching_enabled"] is True


def test_intermediate_priority_still_enables_nightly_stitching(
    _stub_video_and_backend,
) -> None:
    patched = _stub_video_and_backend
    head = {
        "id": "tk-head",
        "player_id": "player-9",
        "team_label": "unknown",
        "track_points": _points(list(range(0, 10)), [i * 5 for i in range(10)]),
    }
    tail = {
        "id": "tk-tail",
        "player_id": None,
        "team_label": "unknown",
        "track_points": _points(list(range(12, 20)), [60 + (i - 12) * 5 for i in range(12, 20)]),
    }
    result = _run([head, tail], SAME_SESSION_PRIORITY - 1)
    assert ("tk-tail", "player-9") in patched
    assert result["reid_strategy"]["stitching_enabled"] is True


def test_reid_strategy_reports_ocr_backend(_stub_video_and_backend) -> None:
    t = {
        "id": "tk", "player_id": None, "team_label": "unknown",
        "track_points": _points([0, 1], [10, 12]),
    }
    # No PARSeq adapter available in tests -> reports the tesseract fallback.
    result = _run([t], NIGHTLY_PRIORITY)
    assert result["reid_strategy"]["ocr"] == "jersey-ocr"
