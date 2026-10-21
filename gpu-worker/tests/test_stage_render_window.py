"""Overlay files must match the reviewed clip and contain trusted callouts."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.stage_render import _metric_callouts, _render, _transcode_to_h264


def test_full_resolution_overlay_renders_only_requested_frame_window(tmp_path):
    from tests.fake_capture import FakeCapture

    cap = FakeCapture(200, width=160, height=90)
    writer = MagicMock()
    writer.isOpened.return_value = True
    with patch("pipeline.hwaccel.nvdec_video_capture", return_value=cap), patch(
        "cv2.VideoWriter", return_value=writer
    ), patch("pipeline.stage_render._transcode_to_h264"):
        _render(Path("source.mp4"), tmp_path / "overlay.mp4", [], [], [], True, 30,
                frame_start=100, frame_end=103)
    # Found by timestamp, and only the requested window is decoded out.
    assert cap.retrieved == [100, 101, 102]
    assert writer.write.call_count == 3
    writer.release.assert_called_once()


def test_overlay_draws_each_box_on_its_own_frame_despite_undecodable_frames(tmp_path):
    from tests.fake_capture import FakeCapture

    # Frame 101 cannot be decoded. Counting reads would draw frame 102's box
    # on the picture of frame 103; by timestamp every box stays on its frame.
    cap = FakeCapture(200, width=160, height=90, missing={101})
    track = {"track_points": [{"frame_number": 102, "bbox": [10, 10, 30, 30]}]}
    drawn: list[int] = []

    def rectangle(frame, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        drawn.append(int(frame[0, 159, 0]))  # FakeCapture paints each frame with its index
        return frame

    writer = MagicMock()
    writer.isOpened.return_value = True
    with patch("pipeline.hwaccel.nvdec_video_capture", return_value=cap), patch(
        "cv2.VideoWriter", return_value=writer
    ), patch("pipeline.stage_render._transcode_to_h264"), patch(
        "pipeline.stage_render.cv2.rectangle", side_effect=rectangle
    ), patch("pipeline.stage_render.draw_field_minimap"):
        _render(Path("source.mp4"), tmp_path / "overlay.mp4", [track], [], [], True, 30,
                frame_start=100, frame_end=104)
    assert 102 in drawn and 103 not in drawn
    assert writer.write.call_count == 4  # the missing frame is held, length is kept


@pytest.mark.parametrize("flags", [
    {"is_suppressed": True}, {"experimental_flag": True},
    {"metric_value": {"value": 8, "provenance": {"validated": False}}},
])
def test_unreviewed_numbers_are_not_burned_into_film(flags):
    metric = {"metric_name": "max_speed", "tracklet_id": "t", "metric_value": {"value": 8}, **flags}
    assert _metric_callouts([{"id": "t"}], [metric]) == {}


def test_missing_browser_encoder_is_an_error_not_a_successful_raw_mp4(tmp_path):
    output = tmp_path / "overlay.mp4"
    with patch("pipeline.stage_render.subprocess.Popen", side_effect=FileNotFoundError):
        with pytest.raises(RuntimeError, match="browser-playable"):
            _transcode_to_h264(tmp_path / "raw.mp4", output)
    assert not output.exists()
