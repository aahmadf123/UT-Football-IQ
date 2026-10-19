"""Physical load for one tracked player on one play.

Built on the smoothed field paths from ``kinematics`` (median filter, moving
average, tracking jumps split out), so a single bad box never becomes a
sprint. Thresholds follow common football GPS practice:

* high-speed running: >= 5.5 yd/s (~11.3 mph)
* sprinting:          >= 7.0 yd/s (~14.3 mph)
* acceleration / deceleration efforts: >= 3 m/s^2 (3.28 yd/s^2)

An effort only counts when it is held for ``MIN_EFFORT_SECONDS``.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from pipeline.metrics.kinematics import MAX_HUMAN_SPEED_YPS, SPEED_SPAN_SECONDS, smoothed_paths

HIGH_SPEED_YPS = 5.5
SPRINT_YPS = 7.0
#: Speed bands (yd/s) for distance splits: walk, jog, run, high speed, sprint.
SPEED_BANDS: tuple[tuple[str, float, float], ...] = (
    ("walk", 0.0, 2.0),
    ("jog", 2.0, 4.0),
    ("run", 4.0, HIGH_SPEED_YPS),
    ("high_speed", HIGH_SPEED_YPS, SPRINT_YPS),
    ("sprint", SPRINT_YPS, float("inf")),
)
ACCEL_YPS2 = 3.28
#: Beyond this the "acceleration" is tracking noise, not a person.
MAX_HUMAN_ACCEL_YPS2 = 10.0
MIN_EFFORT_SECONDS = 0.5
MIN_ACCEL_EFFORT_SECONDS = 0.3
GET_OFF_SECONDS = 1.0


def _speed_series(times: np.ndarray, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Speed (yd/s) at each sample's window midpoint, over ~0.2 s windows."""
    mids: list[float] = []
    values: list[float] = []
    j = 0
    for i in range(len(times)):
        j = max(j, i)
        while j < len(times) - 1 and times[j] - times[i] < SPEED_SPAN_SECONDS:
            j += 1
        dt = times[j] - times[i]
        if dt < SPEED_SPAN_SECONDS * 0.5:
            break
        v = float(np.hypot(*(xy[j] - xy[i])) / dt)
        if v <= MAX_HUMAN_SPEED_YPS:
            mids.append(float((times[i] + times[j]) / 2))
            values.append(v)
    return np.asarray(mids), np.asarray(values)


def _accel_series(t: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Acceleration (yd/s^2) from the speed series, over ~0.2 s windows."""
    mids: list[float] = []
    values: list[float] = []
    j = 0
    for i in range(len(t)):
        j = max(j, i)
        while j < len(t) - 1 and t[j] - t[i] < SPEED_SPAN_SECONDS:
            j += 1
        dt = t[j] - t[i]
        if dt < SPEED_SPAN_SECONDS * 0.5:
            break
        a = float((v[j] - v[i]) / dt)
        if abs(a) <= MAX_HUMAN_ACCEL_YPS2:
            mids.append(float((t[i] + t[j]) / 2))
            values.append(a)
    return np.asarray(mids), np.asarray(values)


def _efforts(t: np.ndarray, flags: np.ndarray, min_seconds: float) -> int:
    """Count runs of consecutive True samples lasting at least ``min_seconds``."""
    count = 0
    start: float | None = None
    previous = 0.0
    for time, flag in zip(t, flags):
        if flag and start is None:
            start = float(time)
        elif not flag and start is not None:
            if previous - start >= min_seconds:
                count += 1
            start = None
        previous = float(time)
    if start is not None and previous - start >= min_seconds:
        count += 1
    return count


def physical_profile(
    points: list[dict[str, Any]], fps: float, snap_time: float | None = None
) -> dict[str, Any] | None:
    """Physical load for one track, or ``None`` with under a second of path.

    ``snap_time`` (seconds, same clock as the points' timestamps) enables the
    snap-relative measures: get-off yards in the first second and time to top
    speed.
    """
    paths = smoothed_paths(points, fps)
    band_yards = {name: 0.0 for name, _, _ in SPEED_BANDS}
    total_time = 0.0
    sprints = high_speed_efforts = accels = decels = 0
    peak_accel = peak_decel = top_speed = 0.0
    top_speed_at: float | None = None
    get_off: float | None = None

    for times, xy in paths:
        if len(times) < 2:
            continue
        t, v = _speed_series(times, xy)
        if len(v) < 2:
            continue
        total_time += float(times[-1] - times[0])
        for i in range(1, len(times)):
            dt = times[i] - times[i - 1]
            step = float(np.hypot(*(xy[i] - xy[i - 1])))
            if dt <= 0 or step / dt > MAX_HUMAN_SPEED_YPS:
                continue
            local = step / dt
            for name, low, high in SPEED_BANDS:
                if low <= local < high:
                    band_yards[name] += step
                    break
        sprints += _efforts(t, v >= SPRINT_YPS, MIN_EFFORT_SECONDS)
        high_speed_efforts += _efforts(t, v >= HIGH_SPEED_YPS, MIN_EFFORT_SECONDS)
        k = int(np.argmax(v))
        if v[k] > top_speed:
            top_speed, top_speed_at = float(v[k]), float(t[k])
        ta, a = _accel_series(t, v)
        if len(a):
            accels += _efforts(ta, a >= ACCEL_YPS2, MIN_ACCEL_EFFORT_SECONDS)
            decels += _efforts(ta, a <= -ACCEL_YPS2, MIN_ACCEL_EFFORT_SECONDS)
            peak_accel = max(peak_accel, float(a.max()))
            peak_decel = max(peak_decel, float(-a.min()))
        if snap_time is not None and get_off is None:
            end_time = snap_time + GET_OFF_SECONDS
            if times[0] <= snap_time and times[-1] >= end_time:
                start_xy = np.asarray(
                    [np.interp(snap_time, times, xy[:, axis]) for axis in range(2)]
                )
                end_xy = np.asarray(
                    [np.interp(end_time, times, xy[:, axis]) for axis in range(2)]
                )
                get_off = float(np.hypot(*(end_xy - start_xy)))

    if total_time < 1.0:
        return None
    yards = sum(band_yards.values())
    profile: dict[str, Any] = {
        "yards": round(yards, 2),
        "seconds": round(total_time, 2),
        "avg_speed_yps": round(yards / total_time, 2),
        "top_speed_yps": round(top_speed, 2),
        "band_yards": {name: round(value, 2) for name, value in band_yards.items()},
        "high_speed_yards": round(band_yards["high_speed"] + band_yards["sprint"], 2),
        "sprint_yards": round(band_yards["sprint"], 2),
        "sprints": sprints,
        "high_speed_efforts": high_speed_efforts,
        "accelerations": accels,
        "decelerations": decels,
        "peak_accel_yps2": round(peak_accel, 2),
        "peak_decel_yps2": round(peak_decel, 2),
    }
    if get_off is not None:
        profile["get_off_yards"] = round(get_off, 2)
    if snap_time is not None and top_speed_at is not None and top_speed_at >= snap_time:
        profile["time_to_top_speed_s"] = round(top_speed_at - snap_time, 2)
    return profile
