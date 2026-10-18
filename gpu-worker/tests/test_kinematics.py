"""Speed/distance from noisy tracks (pipeline.metrics.kinematics)."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.metrics import kinematics

FPS = 30.0


def _track(xs, ys):  # type: ignore[no-untyped-def]
    return [
        {"frame_number": i, "field_x": float(x), "field_y": float(y)}
        for i, (x, y) in enumerate(zip(xs, ys, strict=True))
    ]


def test_jitter_on_a_standing_player_is_not_speed() -> None:
    rng = np.random.default_rng(1)
    n = 90
    track = _track(50 + rng.normal(0, 0.3, n), 10 + rng.normal(0, 0.3, n))
    # Raw frame differences would read ~15 yd/s.
    assert max(kinematics.speeds(track, FPS)) < 2.0
    assert kinematics.distance(track, FPS) < 5.0


def test_steady_run_measures_true_speed() -> None:
    n = 90  # 3 s at 7 yd/s with jitter
    rng = np.random.default_rng(2)
    xs = 20 + 7 * np.arange(n) / FPS + rng.normal(0, 0.2, n)
    track = _track(xs, np.full(n, 5.0))
    speeds = kinematics.speeds(track, FPS)
    assert abs(float(np.median(speeds)) - 7.0) < 0.6
    assert abs(kinematics.distance(track, FPS) - 21.0) < 3.0


def test_impossible_speeds_are_discarded() -> None:
    xs = [10.0] * 30 + [40.0] * 30  # a 30-yard teleport (ID swap)
    speeds = kinematics.speeds(_track(xs, [0.0] * 60), FPS)
    assert all(v <= kinematics.MAX_HUMAN_SPEED_YPS for v in speeds)


def test_persistent_tracking_jump_is_split_before_smoothing() -> None:
    for destination in (15.0, 18.0):
        xs = [10.0] * 30 + [destination] * 30
        track = _track(xs, [0.0] * 60)
        assert max(kinematics.speeds(track, FPS), default=0.0) < 1.0
        assert kinematics.distance(track, FPS) == 0.0


def test_presentation_timestamps_are_used_without_verification_flag() -> None:
    points = [
        {"frame_number": 2, "timestamp_seconds": 0.20, "field_x": 1.0, "field_y": 2.0},
        {"frame_number": 3, "timestamp_seconds": 0.56, "field_x": 5.0, "field_y": 2.0},
    ]
    assert kinematics.speeds(points, FPS) == pytest.approx([4 / 0.36])


def test_contact_speed_uses_the_sample_covering_contact_frame() -> None:
    from pipeline.stage_metrics import _ball_carrier_analytics

    points = [{"frame_number": 0, "field_x": 0.0, "field_y": 0.0}]
    points.extend({"frame_number": frame} for frame in range(1, 21))
    points.extend(
        {
            "frame_number": frame,
            "field_x": 5 * (frame - 21) / FPS,
            "field_y": 0.0,
        }
        for frame in range(21, 61)
    )
    metrics = _ball_carrier_analytics(
        [{"id": "carrier", "track_points": points}],
        snap_frame=0,
        contact_frame=45,
        handoff_frame=None,
        end_frame=None,
        fps=FPS,
    )
    speed = next(metric for metric in metrics if metric["metric_name"] == "speed_at_contact")
    assert speed["metric_value"]["yards_per_second"] == 5.0


def test_gaps_are_never_bridged() -> None:
    points = [
        {"frame_number": 0, "field_x": 10.0, "field_y": 20.0},
        {"frame_number": 3},
        {"frame_number": 6, "field_x": 12.0, "field_y": 20.0},
    ]
    assert kinematics.speeds(points, FPS) == []
    assert kinematics.distance(points, FPS) == 0.0


def test_field_template_uses_ncaa_hash_spacing() -> None:
    # NCAA Rule 1: inbound hash marks 60 ft from each sideline, 40 ft apart.
    # The old template put them 80 ft apart, doubling every lateral distance.
    from pipeline.homography.field_template import HALF_WIDTH, HASH_Y_NORTH, HASH_Y_SOUTH

    assert abs((HASH_Y_NORTH - HASH_Y_SOUTH) - 40 / 3) < 0.01
    assert abs((HALF_WIDTH - HASH_Y_NORTH) - 20.0) < 0.01
