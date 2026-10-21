"""Real VFR media verifies source timing, derivatives and proxy alignment."""

import subprocess
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from pipeline.detection.official_suppressor import OfficialSuppressor
from pipeline.detector_models import StubDetector
from pipeline.stage_detect import _detect
from pipeline.stage_metrics import _frame_speeds
from pipeline.stage_render import _render
from pipeline.video_ingest import LocalFileVideoSource
from pipeline.video_timeline import VideoTimeline


@pytest.fixture
def vfr_video(tmp_path):
    durations = [.08, .12, .36, .04]
    lines = []
    for index, duration in enumerate(durations):
        image = np.full((64, 96, 3), (index + 1) * 40, dtype=np.uint8)
        filename = f"frame-{index}.png"
        assert cv2.imwrite(str(tmp_path / filename), image)
        lines.extend([f"file '{filename}'", f"duration {duration}"])
    lines.append("file 'frame-3.png'")
    manifest = tmp_path / "frames.txt"
    manifest.write_text("\n".join(lines) + "\n")
    output = tmp_path / "vfr.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(manifest), "-fps_mode", "vfr", "-c:v", "libx264", "-bf", "0", "-pix_fmt", "yuv420p",
                    str(output)], check=True, capture_output=True)
    return output


def test_real_pts_survive_source_helpers_and_detector(vfr_video):
    timeline = VideoTimeline.from_path(vfr_video)
    assert timeline.verified
    assert timeline.duration == pytest.approx(.64, abs=1e-6)
    assert timeline.timestamp(3) == pytest.approx(.56, abs=.001)
    assert abs(timeline.timestamp(3) - 3 / timeline.fps) > .05
    assert timeline.frame_for_time(.4) == 2  # frame two is held through .20..56
    source = LocalFileVideoSource(vfr_video)
    assert source.timestamp(3) == pytest.approx(.56, abs=.001)
    assert source.frame_for_time(.4) == 2
    result = _detect(vfr_video, StubDetector(), None, OfficialSuppressor(enabled=False),
                     timeline=timeline)
    assert result["detections"]["3"][0]["timestamp_seconds"] == pytest.approx(.56, abs=.001)
    assert result["detections"]["3"][0]["timestamp_verified"] is True


def test_velocity_uses_elapsed_pts_not_frame_rate(vfr_video):
    timeline = VideoTimeline.from_path(vfr_video)
    points = [{"frame_number": 2, "timestamp_seconds": timeline.timestamp(2), "field_x": 1., "field_y": 2.},
              {"frame_number": 3, "timestamp_seconds": timeline.timestamp(3), "field_x": 5., "field_y": 2.}]
    assert _frame_speeds(points, timeline.fps) == pytest.approx([4 / .36], abs=.001)


def test_vfr_proxy_preserves_held_frame_and_playback_time(vfr_video, tmp_path):
    timeline = VideoTimeline.from_path(vfr_video)
    output = tmp_path / "proxy.mp4"
    mapping = _render(vfr_video, output, [], [], [], False, 25., frame_start=0,
                      frame_end=len(timeline.timestamps), timeline=timeline)
    proxy = LocalFileVideoSource(output)
    frames = list(proxy.iter_frames())
    # A long source gap is displayed as held footage, not compressed to one
    # 1/FPS frame. The actual H264 proxy duration tracks source elapsed time.
    assert len(frames) == round(timeline.duration * 25)
    assert proxy.timeline.duration == pytest.approx(timeline.duration, abs=1 / 25)
    assert mapping["proxy_to_source_frame"][10] == 2  # output .4 sec remains source frame two
    assert float(frames[10][1][30, 90].mean()) == pytest.approx(120., abs=10)


def test_probe_metadata_is_cached_without_caching_pixels(vfr_video):
    first = VideoTimeline.from_path(vfr_video)
    with patch("pipeline.video_timeline.subprocess.run", side_effect=AssertionError("second probe")):
        assert VideoTimeline.from_path(vfr_video) is first


def test_cropped_vfr_proxy_uses_exact_source_frame(vfr_video, tmp_path):
    timeline = VideoTimeline.from_path(vfr_video)
    output = tmp_path / "cropped.mp4"
    mapping = _render(vfr_video, output, [], [], [], False, 25., frame_start=3,
                      frame_end=4, timeline=timeline, start_seconds=.56, end_seconds=.60)
    frames = list(LocalFileVideoSource(output).iter_frames())
    assert len(frames) == 1
    assert mapping["proxy_to_source_frame"] == [3]
    assert float(frames[0][1][30, 90].mean()) == pytest.approx(160., abs=10)


def test_real_cfr_end_does_not_exceed_api_source_duration(tmp_path):
    source = tmp_path / "five-seconds.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=black:s=64x64:r=30:d=5", "-c:v", "libx264", str(source)],
                   check=True, capture_output=True)
    timeline = VideoTimeline.from_path(source)
    # Strict ClipCreate bounds use the recording's measured duration. Rounding
    # 4.966667 + 1/30 formerly proposed 5.000000333333333 and got HTTP 422.
    assert timeline.duration == 5.
    assert timeline.timestamp(len(timeline.timestamps)) <= 5.


def test_ntsc_end_rounding_cannot_extend_the_recording(tmp_path):
    source = tmp_path / "ntsc.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=black:s=64x64:r=30000/1001", "-frames:v", "149",
                    "-c:v", "libx264", str(source)], check=True, capture_output=True)
    timeline = VideoTimeline.from_path(source)
    assert len(timeline.timestamps) == 149
    # Individually rounded final PTS and frame duration can overshoot by one
    # microsecond; the elapsed recording duration must retain the real bound.
    assert timeline.duration <= 149 * 1001 / 30000


def test_long_cfr_film_keeps_its_own_rate_when_probing_fails(tmp_path):
    # A failed probe used to trust the orchestrator's 30.0 placeholder, so
    # 59.94 fps game film got timestamps of frame / 30: twice real time.
    source = tmp_path / "sixty.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=black:s=64x64:r=60000/1001", "-frames:v", "120",
                    "-c:v", "libx264", str(source)], check=True, capture_output=True)
    with patch("pipeline.video_timeline._probe", side_effect=subprocess.TimeoutExpired("ffprobe", 1)):
        timeline = VideoTimeline.from_path(source, fps_fallback=30.)
    assert not timeline.verified
    assert timeline.fps == pytest.approx(59.94, abs=.01)
    assert timeline.timestamp(930) == pytest.approx(930 / 59.94, abs=.001)
    assert timeline.frame_for_time(1.) == 59  # not frame 30


def test_probe_reads_presentation_order_from_packets(tmp_path):
    # B-frames are stored out of presentation order; timestamps must not be.
    source = tmp_path / "bframes.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=s=64x64:r=60000/1001", "-frames:v", "90",
                    "-c:v", "libx264", "-bf", "3", str(source)], check=True, capture_output=True)
    timeline = VideoTimeline.from_path(source)
    assert timeline.verified and len(timeline.timestamps) == 90
    assert timeline.timestamp(60) == pytest.approx(60 * 1001 / 60000, abs=1e-3)


def test_long_film_reads_packet_timestamps_without_decoding(tmp_path, monkeypatch):
    source = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=s=64x64:r=60000/1001", "-frames:v", "300",
                    "-c:v", "libx264", "-bf", "3", str(source)], check=True, capture_output=True)
    monkeypatch.setattr("pipeline.video_timeline.FRAME_PROBE_LIMIT", 100)
    real_run = subprocess.run

    def no_decode(cmd, *args, **kwargs):
        assert "-show_frames" not in cmd
        return real_run(cmd, *args, **kwargs)

    with patch("pipeline.video_timeline.subprocess.run", side_effect=no_decode):
        timeline = VideoTimeline.from_path(source)
    assert timeline.verified and len(timeline.timestamps) == 300
    assert timeline.timestamp(240) == pytest.approx(240 * 1001 / 60000, abs=1e-3)
    assert timeline.duration == pytest.approx(300 * 1001 / 60000, abs=1e-3)
