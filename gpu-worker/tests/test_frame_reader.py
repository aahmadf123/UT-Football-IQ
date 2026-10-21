"""Frames are found by timestamp, so dropped frames never shift the numbering."""

from __future__ import annotations

from typing import Any

from pipeline.video_timeline import VideoTimeline, iter_frames, read_frames, seek_to
from tests.fake_capture import FakeCapture

FPS = 30.0


def _timeline(frames: int) -> VideoTimeline:
    return VideoTimeline(tuple(i / FPS for i in range(frames)), frames / FPS, FPS)


def _ids(pairs: list[tuple[int, Any]]) -> list[tuple[int, int]]:
    return [(index, int(frame[0, 0, 0])) for index, frame in pairs]


def test_dropped_frames_keep_true_numbers() -> None:
    cap = FakeCapture(400, fps=FPS, missing={5, 6, 200})
    got = list(iter_frames(cap, _timeline(400), 0, None, fill_gaps=False))
    numbers = [index for index, _ in got]
    assert 5 not in numbers and 200 not in numbers
    # Counting reads would call frame 300 "297"; by timestamp it is 300.
    assert dict(_ids(got))[300] == 300 % 256


def test_gaps_are_filled_for_rendering() -> None:
    cap = FakeCapture(20, fps=FPS, missing={11})
    got = _ids(list(iter_frames(cap, _timeline(20), 10, 14)))
    assert got == [(10, 10), (11, 12), (12, 12), (13, 13)]  # 11 shows the next good picture


def test_wanted_frames_only_are_retrieved() -> None:
    cap = FakeCapture(60, fps=FPS)
    got = list(iter_frames(cap, _timeline(60), 0, None, wanted=lambda i: i % 2 == 0, fill_gaps=False))
    assert [i for i, _ in got] == list(range(0, 60, 2))
    assert cap.retrieved == list(range(0, 60, 2))


def test_read_frames_returns_exact_frames_and_skips_undecodable() -> None:
    cap = FakeCapture(3000, fps=FPS, missing={1500})
    got = read_frames(cap, _timeline(3000), [2900, 40, 1500, 41, 1000])
    assert sorted(got) == [40, 41, 1000, 2900]
    assert all(int(frame[0, 0, 0]) == index % 256 for index, frame in got.items())


def test_seek_lands_on_the_next_good_frame() -> None:
    cap = FakeCapture(100, fps=FPS, missing={50})
    assert seek_to(cap, _timeline(100), 50) == 51
    assert seek_to(cap, _timeline(100), 10) == 10
    assert seek_to(cap, _timeline(100), 150) is None


def test_a_capture_that_never_ends_stops_at_the_timeline_end() -> None:
    class Endless(FakeCapture):
        def grab(self) -> bool:
            self._current = self._next
            self._next += 1
            return True

    got = list(iter_frames(Endless(10**9, fps=FPS), _timeline(50), 0, None, fill_gaps=False))
    assert len(got) == 50
