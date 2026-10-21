"""Native frames survive sub-output-frame clips and closely spaced source PTS."""

from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from pipeline import backend, storage
from pipeline.playback_proxy import PlaybackProxySession
from pipeline.video_timeline import VideoTimeline, current_index


@pytest.mark.parametrize("ticks", [1, 1000, 1001])
def test_positive_native_window_has_one_proxy_picture(ticks):
    # 984985/60000 and 985985/60000 are actual measured game-window ticks.
    start = float(Fraction(984985, 60000))
    end = float(Fraction(984985 + ticks, 60000))
    timeline = VideoTimeline((0.0, start, end), end + 0.1, 58.94)
    first, last = timeline.window(start, end)
    assert (first, last) == (1, 2)
    mapping = timeline.proxy_mapping(first, last, 30, start_seconds=start, end_seconds=end)
    assert mapping["proxy_to_source_frame"] == [1]
    assert mapping["proxy_to_source_seconds"] == [start]
    assert mapping["source_start_seconds"] == start
    assert mapping["source_end_seconds"] == end


def test_empty_source_window_does_not_invent_a_proxy_frame():
    timeline = VideoTimeline((0.0, 0.1), 0.2, 10)
    assert timeline.proxy_mapping(1, 1, 30, start_seconds=0.1, end_seconds=0.11)[
        "proxy_to_source_frame"
    ] == []


def test_playback_floor_does_not_jump_over_a_distinct_native_tick():
    timeline = VideoTimeline((0.0, 0.0000005, 0.02), 0.04, 50)
    assert timeline.frame_for_time(0.0) == 0
    assert timeline.window(0.0, 0.0000005) == (0, 1)


def test_decoder_timestamp_lookup_distinguishes_adjacent_native_ticks():
    timestamps = tuple(float(Fraction(tick, 60000)) for tick in (983983, 983984, 984985))
    timeline = VideoTimeline(timestamps, timestamps[-1] + 0.1, 58.94)
    for expected, seconds in enumerate(timestamps):
        capture = SimpleNamespace(get=lambda _prop, value=seconds: value * 1000)
        assert current_index(capture, timeline) == expected


def test_real_one_frame_source_encodes_playable_proxy(tmp_path: Path, monkeypatch):
    source = tmp_path / "one-frame-source.mp4"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 60, (224, 128))
    assert writer.isOpened()
    for index in range(60):
        writer.write(np.full((128, 224, 3), 60 + index, dtype=np.uint8))
    writer.release()
    monkeypatch.setattr(storage, "LOCAL_STORAGE_ROOT", str(tmp_path / "library"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.delenv("PIPELINE_STORAGE_TRANSPORT", raising=False)
    monkeypatch.setattr(backend, "BACKEND_API_URL", "")
    timeline = VideoTimeline.from_path(source)
    start, end = timeline.timestamp(1), timeline.timestamp(2)
    session = PlaybackProxySession(source, timeline)
    try:
        result = session.run("short-native-window", start, end)
    finally:
        session.close()
    assert result["proxy_timeline"]["proxy_to_source_frame"] == [1]
    assert result["playback"]["source_start_seconds"] == start
    assert result["playback"]["source_end_seconds"] == end
    assert result["playback"]["alignment_validation"]["passed"]
    assert result["playback"]["duration_validation"]["passed"]
    encoded = storage.download_to_temp(result["playback_uri"])
    capture = cv2.VideoCapture(str(encoded))
    try:
        assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 1
        assert capture.read()[0]
    finally:
        capture.release()
        encoded.unlink()
