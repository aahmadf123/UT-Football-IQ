from __future__ import annotations

import numpy as np
import pytest

from pipeline.metrics.physical import physical_profile

FPS = 30.0


def _track(speed_at, seconds: float, noise: float = 0.0, seed: int = 0) -> list[dict]:
    """Straight-line run along x with speed ``speed_at(t)`` (yd/s)."""
    rng = np.random.default_rng(seed)
    points, x = [], 10.0
    for frame in range(int(seconds * FPS) + 1):
        t = frame / FPS
        if frame:
            x += speed_at(t) / FPS
        points.append(
            {
                "frame_number": frame,
                "field_x": x + rng.normal(0, noise),
                "field_y": 20.0 + rng.normal(0, noise),
            }
        )
    return points


def _burst(t: float) -> float:
    """Still for 1 s, accelerate at 4 yd/s^2 to 8 yd/s, hold, then stop."""
    if t < 1.0:
        return 0.0
    if t < 3.0:
        return 4.0 * (t - 1.0)
    if t < 4.5:
        return 8.0
    return max(0.0, 8.0 - 5.0 * (t - 4.5))


def test_a_burst_counts_one_sprint_and_both_efforts():
    profile = physical_profile(_track(_burst, 7.0), FPS, snap_time=1.0)
    assert profile is not None
    assert profile["top_speed_yps"] == pytest.approx(8.0, abs=0.4)
    assert profile["sprints"] == 1
    assert profile["high_speed_efforts"] == 1
    assert profile["accelerations"] == 1
    assert profile["decelerations"] == 1
    assert profile["peak_accel_yps2"] == pytest.approx(4.0, abs=0.8)
    assert profile["peak_decel_yps2"] == pytest.approx(5.0, abs=1.0)
    # 0.5 * 4 * 1^2 = 2 yd in the first second after the snap.
    assert profile["get_off_yards"] == pytest.approx(2.0, abs=0.5)
    assert profile["time_to_top_speed_s"] == pytest.approx(2.5, abs=1.0)
    bands = profile["band_yards"]
    assert sum(bands.values()) == pytest.approx(profile["yards"], abs=0.05)
    assert profile["sprint_yards"] == pytest.approx(bands["sprint"])
    assert bands["sprint"] > 8.0  # >= 1.5 s at 8 yd/s, plus the ramp above 7


def test_steady_jog_has_no_efforts():
    profile = physical_profile(_track(lambda t: 3.0, 5.0), FPS)
    assert profile is not None
    assert profile["sprints"] == 0
    assert profile["high_speed_efforts"] == 0
    assert profile["accelerations"] == 0
    assert profile["decelerations"] == 0
    assert profile["band_yards"]["jog"] == pytest.approx(profile["yards"], rel=0.05)
    assert "get_off_yards" not in profile


def test_box_jitter_does_not_become_sprints():
    profile = physical_profile(_track(lambda t: 0.0, 5.0, noise=0.15), FPS)
    assert profile is not None
    assert profile["sprints"] == 0
    assert profile["accelerations"] == 0
    assert profile["top_speed_yps"] < 2.0


def test_brief_track_has_no_profile():
    assert physical_profile(_track(lambda t: 5.0, 0.5), FPS) is None
