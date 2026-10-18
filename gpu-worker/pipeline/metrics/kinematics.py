"""Speed and distance from noisy tracked positions.

Raw frame-to-frame differences turn a few pixels of box jitter into absurd
speeds: on Toledo drone film at 30 detections/s the per-tracklet "max speed"
had a median of 16 yd/s and a max of 82 yd/s (elite players top out near
10-11 yd/s). Every speed/distance metric goes through here instead:

1. a running median removes single-frame position spikes (an ID swap or a
   calibration jump for one frame);
2. a centred moving average over ~0.5 s smooths the path;
3. speed is measured across a ~0.2 s span, not between adjacent frames;
4. anything faster than a human can run is discarded as a tracking error.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

#: No football player sustains more than ~11.5 yd/s (23.5 mph).
MAX_HUMAN_SPEED_YPS = 11.5
SMOOTH_SECONDS = 0.5
SPEED_SPAN_SECONDS = 0.2
MEDIAN_WINDOW = 5


def _field_xy(point: dict[str, Any]) -> tuple[float, float] | None:
    try:
        x, y = float(point["field_x"]), float(point["field_y"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _times(points: list[dict[str, Any]], fps: float) -> np.ndarray:
    """Seconds for each point: presentation time when present, else frame/fps."""
    out = []
    for p in points:
        ts = p.get("timestamp_seconds")
        try:
            seconds = float(ts)
        except (TypeError, ValueError):
            seconds = math.nan
        if math.isfinite(seconds):
            out.append(seconds)
        else:
            out.append(float(p.get("frame_number", 0)) / (fps or 30.0))
    return np.asarray(out, dtype=float)


def _runs(points: list[dict[str, Any]]) -> list[list[tuple[dict[str, Any], tuple[float, float]]]]:
    """Unbroken stretches of points that have field coordinates.

    A point without coordinates ends a stretch: motion is never invented
    across a calibration gap.
    """
    runs: list[list[tuple[dict[str, Any], tuple[float, float]]]] = [[]]
    for point in sorted(points, key=lambda p: p.get("frame_number", 0)):
        xy = _field_xy(point)
        if xy is None:
            if runs[-1]:
                runs.append([])
            continue
        runs[-1].append((point, xy))
    return [run for run in runs if run]


def _smooth(
    run: list[tuple[dict[str, Any], tuple[float, float]]], fps: float
) -> tuple[np.ndarray, np.ndarray]:
    times = _times([p for p, _ in run], fps)
    xy = np.asarray([xy for _, xy in run], dtype=float)
    n = len(xy)
    if n >= MEDIAN_WINDOW:
        half = MEDIAN_WINDOW // 2
        padded = np.pad(xy, ((half, half), (0, 0)), mode="edge")
        xy = np.stack([np.median(padded[i : i + MEDIAN_WINDOW], axis=0) for i in range(n)])
    span = times[-1] - times[0] if n > 1 else 0.0
    samples_per_second = (n - 1) / span if span > 0 else fps
    half = max(1, int(round(SMOOTH_SECONDS * samples_per_second / 2)))
    if n > 2 * half + 1:
        kernel = np.ones(2 * half + 1) / (2 * half + 1)
        padded = np.pad(xy, ((half, half), (0, 0)), mode="edge")
        xy = np.stack(
            [np.convolve(padded[:, axis], kernel, mode="valid") for axis in range(2)], axis=1
        )
    return times, xy


def _split_tracking_jumps(
    run: list[tuple[dict[str, Any], tuple[float, float]]], fps: float
) -> list[list[tuple[dict[str, Any], tuple[float, float]]]]:
    """Split persistent impossible displacements before smoothing can hide them."""
    window = MEDIAN_WINDOW
    if len(run) < 2 * window:
        return [run]

    times = _times([point for point, _ in run], fps)
    xy = np.asarray([position for _, position in run], dtype=float)
    boundaries = [0]
    boundary = window
    while boundary <= len(run) - window:
        before = xy[boundary - window : boundary]
        after = xy[boundary : boundary + window]
        before_position = np.median(before, axis=0)
        after_position = np.median(after, axis=0)
        before_time = float(np.median(times[boundary - window : boundary]))
        onset_elapsed = times[boundary] - before_time
        persistence_elapsed = (
            float(np.median(times[boundary : boundary + window])) - times[boundary]
        )
        displacement = float(np.linalg.norm(xy[boundary] - before_position))
        if onset_elapsed > 0 and persistence_elapsed > 0:
            onset_speed = displacement / onset_elapsed
            persistence_speed = (
                float(np.linalg.norm(after_position - xy[boundary])) / persistence_elapsed
            )
        else:
            onset_speed = persistence_speed = 0.0
        if (
            displacement > SPEED_SPAN_SECONDS * MAX_HUMAN_SPEED_YPS
            and onset_speed > MAX_HUMAN_SPEED_YPS
            and persistence_speed <= MAX_HUMAN_SPEED_YPS
        ):
            boundaries.append(boundary)
            boundary += window
        else:
            boundary += 1

    boundaries.append(len(run))
    return [run[start:end] for start, end in zip(boundaries, boundaries[1:]) if start < end]


def smoothed_paths(points: list[dict[str, Any]], fps: float) -> list[tuple[np.ndarray, np.ndarray]]:
    """``(times, xy)`` for each unbroken, smoothed stretch of the track."""
    return [
        _smooth(stretch, fps)
        for run in _runs(points)
        for stretch in _split_tracking_jumps(run, fps)
    ]


def speed_samples(points: list[dict[str, Any]], fps: float) -> list[tuple[int, int, float]]:
    """Plausible ``(start_frame, end_frame, speed)`` samples over ~0.2 s."""
    out: list[tuple[int, int, float]] = []
    for run in _runs(points):
        for stretch in _split_tracking_jumps(run, fps):
            times, xy = _smooth(stretch, fps)
            frames = [int(point.get("frame_number", 0)) for point, _ in stretch]
            run_samples: list[tuple[int, int, float]] = []
            for i in range(len(times)):
                j = i
                while j < len(times) - 1 and times[j] - times[i] < SPEED_SPAN_SECONDS:
                    j += 1
                dt = times[j] - times[i]
                if dt < SPEED_SPAN_SECONDS * 0.5:
                    continue
                v = float(np.hypot(*(xy[j] - xy[i])) / dt)
                if v <= MAX_HUMAN_SPEED_YPS:
                    run_samples.append((frames[i], frames[j], v))
            out.extend(run_samples)

            if len(times) > 1 and not any(end == frames[-1] for _, end, _ in run_samples):
                prior = [
                    i
                    for i in range(len(times) - 1)
                    if SPEED_SPAN_SECONDS * 0.5 <= times[-1] - times[i]
                ]
                if prior:
                    i = min(
                        prior,
                        key=lambda index: abs((times[-1] - times[index]) - SPEED_SPAN_SECONDS),
                    )
                    dt = times[-1] - times[i]
                    v = float(np.hypot(*(xy[-1] - xy[i])) / dt)
                    if v <= MAX_HUMAN_SPEED_YPS:
                        out.append((frames[i], frames[-1], v))
    return out


def speeds(points: list[dict[str, Any]], fps: float) -> list[float]:
    """Plausible speeds (yd/s) along the smoothed path, each over ~0.2 s."""
    return [speed for _, _, speed in speed_samples(points, fps)]


def distance(points: list[dict[str, Any]], fps: float) -> float:
    """Length (yd) of the smoothed path, ignoring impossible jumps and never
    bridging a stretch without coordinates."""
    total = 0.0
    for times, xy in smoothed_paths(points, fps):
        for i in range(1, len(times)):
            step = float(np.hypot(*(xy[i] - xy[i - 1])))
            dt = times[i] - times[i - 1]
            if dt > 0 and step / dt <= MAX_HUMAN_SPEED_YPS:
                total += step
    return total
