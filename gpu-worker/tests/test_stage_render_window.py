"""Overlay files must match the reviewed clip and contain trusted callouts."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from pipeline.stage_render import _metric_callouts, _render, _transcode_to_h264


def test_full_resolution_overlay_renders_only_requested_frame_window(tmp_path):
    cap = MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda key: {cv2.CAP_PROP_FRAME_WIDTH: 160, cv2.CAP_PROP_FRAME_HEIGHT: 90}.get(key, 0)
    cap.read.return_value = True, np.zeros((90, 160, 3), np.uint8)
    writer = MagicMock()
    writer.isOpened.return_value = True
    with patch("pipeline.hwaccel.nvdec_video_capture", return_value=cap), patch(
        "cv2.VideoWriter", return_value=writer
    ), patch("pipeline.stage_render._transcode_to_h264"):
        _render(Path("source.mp4"), tmp_path / "overlay.mp4", [], [], [], True, 30,
                frame_start=100, frame_end=103)
    cap.set.assert_called_once_with(cv2.CAP_PROP_POS_FRAMES, 100)
    assert cap.read.call_count == 3
    assert writer.write.call_count == 3
    cap.release.assert_called_once()
    writer.release.assert_called_once()


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
