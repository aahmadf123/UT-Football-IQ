"""Speed/distance from noisy tracks (pipeline.metrics.kinematics)."""

import os
import sys

import numpy as np

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
