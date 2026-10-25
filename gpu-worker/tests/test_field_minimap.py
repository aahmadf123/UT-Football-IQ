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
    # Compact bottom-right inset: 200px map, 12px margin, opaque observations.
    center = field_to_minimap(50, 0, (748, 439, 200, 89))
    assert tuple(frame[center[1], center[0]]) == (0, 205, 255)
    assert not frame[:10, :100].any()  # the source canvas outside the inset is unchanged


def test_solid_inset_remains_readable_independent_of_underlying_footage() -> None:
    dark = np.zeros((540, 960, 3), dtype=np.uint8)
    bright = np.full_like(dark, 255)
    for frame in (dark, bright):
        draw_field_minimap(frame, [])
    assert np.array_equal(dark[436:532, 745:952], bright[436:532, 745:952])
    assert tuple(bright[100, 100]) == (255, 255, 255)
    assert tuple(dark[100, 100]) == (0, 0, 0)


def test_small_video_inset_stays_within_source_canvas() -> None:
    frame = np.full((48, 64, 3), 255, dtype=np.uint8)
    draw_field_minimap(frame, [{"field_x": float("nan"), "field_y": 0}])
    assert tuple(frame[0, 0]) == (255, 255, 255)


def test_official_candidates_are_not_drawn_as_player_positions() -> None:
    empty = np.zeros((540, 960, 3), dtype=np.uint8)
    official = empty.copy()
    draw_field_minimap(empty, [])
    draw_field_minimap(
        official,
        [
            {"field_x": 50, "field_y": 0, "team_label": "official"},
            {"field_x": 30, "field_y": 2, "actor_kind": "official"},
            {"field_x": 30, "field_y": 2, "actor_kind": "sideline"},
            {"field_x": 30, "field_y": 2, "actor_kind": "unknown"},
        ],
    )
    assert np.array_equal(empty, official)


@pytest.mark.parametrize("analytics_safe", [True, False])
def test_period_export_maps_only_fresh_calibrated_tracks(
    tmp_path: Path, analytics_safe: bool
) -> None:
    track = {
        "team_label": "offense",
        "track_points": [
            {
                "frame_number": 100,
                "bbox": [10, 10, 30, 50],
                "field_x": 60,
                "field_y": 20,
                "shot_id": 1,
            },
            {
                "frame_number": 102,
                "bbox": [12, 10, 32, 50],
                "field_x": 62,
                "field_y": 20,
                "shot_id": 1,
            },
        ],
    }
    observed = []
    with (
        patch(
            "pipeline.hwaccel.nvdec_video_capture",
            return_value=FakeCapture(200, width=160, height=90),
        ),
        patch("cv2.VideoWriter", return_value=MagicMock()),
        patch("renderer.period_renderer._transcode_h264"),
        patch(
            "renderer.period_renderer.draw_field_minimap",
            side_effect=lambda _frame, points: observed.append(points),
        ),
    ):
        official = {**track, "provenance": {"actor_kind": "official"}}
        _render_reduced(
            Path("source.mp4"),
            tmp_path / "overlay.mp4",
            [track, official],
            [],
            analytics_safe,
            30,
            frame_start=100,
            frame_end=103,
        )
    if analytics_safe:
        assert len(observed) == 3
        assert all(len(points) == 1 for points in observed)
        assert observed[1][0]["field_x"] == 61
        assert observed[1][0]["interpolated"] is True
    else:
        assert observed == []


@pytest.mark.parametrize("actor_kind", ["official", "sideline", "unknown", None])
def test_full_render_maps_legacy_unknown_team_but_never_explicit_nonplayers(tmp_path, actor_kind):
    from pipeline.stage_render import _render

    track = {
        "id": "person",
        "team_label": "unknown",
        "provenance": {},
        "track_points": [
            {
                "frame_number": i,
                "timestamp_seconds": i / 30,
                "bbox": [10, 10, 30, 50],
                "field_x": 50 + i,
                "field_y": 0,
                "shot_id": "0",
                "visibility_state": "observed",
            }
            for i in range(2)
        ],
    }
    if actor_kind:
        track.update({"class": actor_kind, "provenance": {"actor_kind": actor_kind}})
    observed = []
    with (
        patch(
            "pipeline.hwaccel.nvdec_video_capture",
            return_value=FakeCapture(2, width=160, height=90),
        ),
        patch("cv2.VideoWriter", return_value=MagicMock()),
        patch("pipeline.stage_render._transcode_to_h264"),
        patch(
            "pipeline.stage_render.draw_field_minimap",
            side_effect=lambda _frame, points: observed.append(points),
        ),
    ):
        _render(
            Path("source.mp4"),
            tmp_path / "overlay.mp4",
            [track],
            [],
            [],
            True,
            30,
            frame_start=0,
            frame_end=2,
        )
    assert len(observed) == 2
    assert all(len(points) == (1 if actor_kind is None else 0) for points in observed)
