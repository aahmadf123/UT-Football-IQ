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


def _mock_native_probe(monkeypatch, stream, rows, *, packets=False):
    import pipeline.video_timeline as module

    module._probe.cache_clear()
    requests = []

    def probe(_path, *args, **_kwargs):
        requests.append(args)
        if args[1].startswith("stream="):
            return {"streams": [stream]}
        return {"packets" if packets else "frames": rows}

    monkeypatch.setattr(module, "_ffprobe_json", probe)
    timeline = module._probe("native-clock-protocol", 1, 1)
    return timeline, requests


@pytest.mark.parametrize("packets", [False, True])
def test_probe_prefers_native_ticks_over_rounded_six_decimal_strings(monkeypatch, packets):
    # Actual canary clock values; pictures/media are not needed to test ffprobe parsing.
    ticks = [0, 983983, 983984, 984985, 985985]
    rows = [{"pts": tick, "pts_time": f"{tick/60000:.6f}",
             "best_effort_timestamp": tick, "best_effort_timestamp_time": f"{tick/60000:.6f}",
             "duration": 1001, "duration_time": "0.016683"} for tick in ticks]
    if packets:
        rows = [rows[index] for index in (0, 3, 1, 4, 2)]  # decode order may differ
    stream = {"avg_frame_rate":"60000/1001", "time_base":"1/60000", "duration":"16.449767",
              "duration_ts":986986, "nb_frames":"30000" if packets else "5"}
    timeline, requests = _mock_native_probe(monkeypatch, stream, rows, packets=packets)
    assert timeline.timestamp(3) == 984985/60000
    assert timeline.timestamp(2) > timeline.timestamp(1)
    assert timeline.timestamp(2)-timeline.timestamp(1) == pytest.approx(1/60000, abs=1e-15)
    assert "time_base" in requests[0][1]
    assert "pts" in requests[1][1 if packets else 2]
    assert not packets or all("-show_frames" not in request for request in requests)


def test_probe_normalizes_integer_origin_before_float_conversion(monkeypatch):
    origin = 2**55
    ticks = [origin, origin+1, origin+1002]
    rows = [{"best_effort_timestamp":tick, "best_effort_timestamp_time":f"{tick/60000:.6f}",
             "pkt_duration":1001} for tick in ticks]
    stream = {"avg_frame_rate":"60000/1001", "time_base":"1/60000", "nb_frames":"3"}
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows)
    assert timeline.timestamps == (0., 1/60000, 1002/60000)
    assert timeline.duration == 2003/60000
    assert timeline.pts_origin == origin/60000


@pytest.mark.parametrize("duration_key", ["duration", "pkt_duration"])
def test_probe_preserves_exact_stream_and_frame_duration_ticks(monkeypatch, duration_key):
    rows = [{"best_effort_timestamp": tick, "best_effort_timestamp_time": f"{tick/60000:.6f}",
             duration_key:1, "duration_time":"0.000017"} for tick in (0,1)]
    stream = {"avg_frame_rate":"60000/1", "time_base":"1/60000", "nb_frames":"2",
              "duration_ts":2, "duration":"0.000033"}
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows)
    assert timeline.timestamp(1) == 1/60000
    assert timeline.duration == 2/60000


@pytest.mark.parametrize("ticks", [[0,1,1], [0,2,1]])
def test_probe_rejects_actual_duplicate_or_out_of_order_ticks(monkeypatch, ticks):
    rows = [{"best_effort_timestamp":tick, "best_effort_timestamp_time":str(index/30)}
            for index,tick in enumerate(ticks)]
    stream = {"avg_frame_rate":"30/1", "time_base":"1/60000", "nb_frames":"3"}
    with pytest.raises(ValueError, match="strictly ordered"):
        _mock_native_probe(monkeypatch, stream, rows)


def test_probe_uses_pts_then_decimal_only_when_exact_fields_are_unavailable(monkeypatch):
    stream = {"avg_frame_rate":"30/1", "time_base":"1/60000", "nb_frames":"3"}
    rows = [{"best_effort_timestamp":60000, "best_effort_timestamp_time":"1.000000"},
            {"best_effort_timestamp":"N/A", "pts":60001, "best_effort_timestamp_time":"1.000017"},
            {"best_effort_timestamp_time":"1.033333", "duration_time":"0.033333"}]
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows)
    assert timeline.timestamps == (0.,1/60000,.033333)


def test_probe_uses_actual_frame_pts_before_decoder_best_effort(monkeypatch):
    stream = {"avg_frame_rate":"60000/1001", "time_base":"1/60000", "nb_frames":"3"}
    rows = [{"pts":tick, "best_effort_timestamp":estimate,
             "pts_time":f"{tick/60000:.6f}", "best_effort_timestamp_time":f"{estimate/60000:.6f}"}
            for tick,estimate in [(0,0),(1001,1002),(1002,1002)]]
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows)
    assert timeline.timestamps == (0.,1001/60000,1002/60000)


@pytest.mark.parametrize("ticks", [[0,1,1], [0,2,1]])
def test_probe_does_not_mask_bad_actual_pts_with_best_effort(monkeypatch, ticks):
    stream = {"avg_frame_rate":"30/1", "time_base":"1/60000", "nb_frames":"3"}
    rows = [{"pts":tick,"best_effort_timestamp":index,"best_effort_timestamp_time":str(index/30)}
            for index,tick in enumerate(ticks)]
    with pytest.raises(ValueError, match="strictly ordered"):
        _mock_native_probe(monkeypatch, stream, rows)


def test_probe_rejects_duplicate_packet_ticks_even_when_decimals_differ(monkeypatch):
    stream = {"avg_frame_rate":"30/1", "time_base":"1/60000", "nb_frames":"30000"}
    rows = [{"pts":tick,"pts_time":str(index/30)} for index,tick in enumerate([0,1,1])]
    with pytest.raises(ValueError, match="strictly ordered"):
        _mock_native_probe(monkeypatch, stream, rows, packets=True)


@pytest.mark.parametrize("time_base", [None, "N/A", "0/1"])
def test_probe_decimal_compatibility_without_usable_time_base(monkeypatch, time_base):
    stream = {"avg_frame_rate":"30/1", "nb_frames":"2"}
    if time_base is not None:
        stream['time_base'] = time_base
    rows = [{"pts":tick,"pts_time":clock,"duration_time":".033333"}
            for tick,clock in [(60000,"1.000000"),(62000,"1.033333")]]
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows)
    assert timeline.timestamps == (0.,.033333)


def test_probe_excludes_explicitly_discarded_packet_preroll_before_origin(monkeypatch):
    stream = {"avg_frame_rate":"60000/1001", "time_base":"1/60000", "nb_frames":"30000", "duration_ts":2002}
    rows = [{"pts":-2002,"flags":"_D_"},{"pts":-1001,"flags":"_D_"},
            {"pts":0,"flags":"K__"},{"pts":1001,"flags":"___"}]
    timeline, requests = _mock_native_probe(monkeypatch, stream, rows, packets=True)
    assert timeline.timestamps == (0.,1001/60000)
    assert timeline.pts_origin == 0.
    assert timeline.duration == 2002/60000
    assert "flags" in requests[1][1]


def test_probe_ignores_missing_pts_only_on_explicitly_discarded_packets(monkeypatch):
    stream = {"avg_frame_rate":"30/1", "time_base":"1/60000", "nb_frames":"30000"}
    rows = [{"flags":"_D_"},{"pts":"N/A","pts_time":"N/A","flags":"KD_"},
            {"pts":0,"flags":"K__"},{"pts":2000,"flags":"___"}]
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows, packets=True)
    assert timeline.timestamps == (0.,2000/60000)


@pytest.mark.parametrize("flags", [None, "___", "K__"])
def test_probe_preserves_nondiscard_negative_packet_origins(monkeypatch, flags):
    stream = {"avg_frame_rate":"60000/1001", "time_base":"1/60000", "nb_frames":"30000"}
    rows = [{"pts":tick} for tick in [-1001,0,1001]]
    if flags is not None:
        for row in rows:
            row['flags'] = flags
    timeline, _ = _mock_native_probe(monkeypatch, stream, rows, packets=True)
    assert timeline.timestamps == (0.,1001/60000,2002/60000)
    assert timeline.pts_origin == -1001/60000


@pytest.mark.parametrize("flags", [None, "___", "K__"])
def test_probe_still_refuses_missing_nondiscard_packet_pts(monkeypatch, flags):
    stream = {"avg_frame_rate":"30/1", "time_base":"1/60000", "nb_frames":"30000"}
    bad = {"flags":flags} if flags is not None else {}
    with pytest.raises(ValueError, match="packets without usable"):
        _mock_native_probe(monkeypatch, stream, [{"pts":0},bad], packets=True)
