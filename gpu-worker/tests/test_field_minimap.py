"""The video inset uses real NCAA yards and disappears without calibration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from renderer.field_minimap import FIELD_WIDTH_YD, draw_field_minimap, field_to_minimap
from renderer.period_renderer import _render_reduced
from tests.fake_capture import FakeCapture


def test_field_corners_and_center_map_to_the_correct_inset_coordinates() -> None:
    rectangle = (20, 30, 240, 106)
    assert field_to_minimap(-10, -FIELD_WIDTH_YD / 2, rectangle) == (20, 30)
    assert field_to_minimap(110, FIELD_WIDTH_YD / 2, rectangle) == (260, 136)
    assert field_to_minimap(50, 0, rectangle) == (140, 83)
    assert field_to_minimap(50, -10, rectangle)[1] < 83
    assert field_to_minimap(50, 10, rectangle)[1] > 83
    assert field_to_minimap(float("nan"), 20, rectangle) is None
    assert field_to_minimap(121, 20, rectangle) is None


def test_minimap_draws_observed_position_and_ncaa_hashes() -> None:
    frame = np.zeros((540, 960, 3), dtype=np.uint8)
    draw_field_minimap(frame, [{"field_x": 50, "field_y": 0, "team_label": "offense"}])
    # Quiet bottom-right inset: 200px map, 12px margin and35% blended paint.
    center = field_to_minimap(50, 0, (748, 439, 200, 89))
    assert tuple(frame[center[1], center[0]]) == (0, 72, 89)
    assert not frame[:10, :100].any()  # the source canvas outside the inset is unchanged


@pytest.mark.parametrize("analytics_safe", [True, False])
def test_period_export_maps_only_fresh_calibrated_tracks(tmp_path: Path, analytics_safe: bool) -> None:
    track = {"team_label": "offense", "track_points": [
        {"frame_number": 100, "bbox": [10, 10, 30, 50], "field_x": 60, "field_y": 20, "shot_id": 1},
        {"frame_number": 102, "bbox": [12, 10, 32, 50], "field_x": 62, "field_y": 20, "shot_id": 1},
    ]}
    observed = []
    with patch("pipeline.hwaccel.nvdec_video_capture", return_value=FakeCapture(200, width=160, height=90)), patch(
        "cv2.VideoWriter", return_value=MagicMock()
    ), patch("renderer.period_renderer._transcode_h264"), patch(
        "renderer.period_renderer.draw_field_minimap", side_effect=lambda _frame, points: observed.append(points)
    ):
        _render_reduced(Path("source.mp4"), tmp_path / "overlay.mp4", [track], [],
                        analytics_safe, 30, frame_start=100, frame_end=103)
    if analytics_safe:
        assert len(observed) == 3
        assert observed[1][0]["field_x"] == 61
        assert observed[1][0]["interpolated"] is True
    else:
        assert observed == []
