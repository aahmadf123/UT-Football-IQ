"""Small presentation-time metadata, independent of decoded image buffers.

Frame indices address images; seconds address playback. They coincide through
FPS only on constant-frame-rate media. ffprobe PTS are cached once per source.
"""

from __future__ import annotations

import bisect
import json
import math
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pipeline.runtime import check_control


@dataclass(frozen=True, slots=True)
class VideoTimeline:
    timestamps: tuple[float, ...]
    duration: float
    fps: float
    pts_origin: float = 0.0
    verified: bool = True

    @property
    def is_vfr(self) -> bool:
        intervals = [b - a for a, b in zip(self.timestamps, self.timestamps[1:])]
        return bool(intervals) and max(intervals) - min(intervals) > 1e-4

    @classmethod
    def from_path(cls, path: Path | str, *, fps_fallback: float = 30.,
                  frame_count_fallback: int = 0) -> VideoTimeline:
        source = Path(path)
        try:
            stat = source.stat()
            return _probe(str(source.resolve()), stat.st_size, stat.st_mtime_ns)
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError,
                json.JSONDecodeError):
            fps = fps_fallback if fps_fallback > 0 else 30.
            return cls(tuple(i / fps for i in range(frame_count_fallback)),
                       frame_count_fallback / fps, fps, verified=False)

    def timestamp(self, frame: int) -> float:
        if frame == len(self.timestamps):
            return self.duration
        if 0 <= frame < len(self.timestamps):
            return self.timestamps[frame]
        if not self.verified:
            return frame / self.fps
        raise IndexError(f"Frame {frame} is outside the source timeline")

    def frame_for_time(self, seconds: float) -> int:
        """Index of the frame displayed at this playback time (floor)."""
        if not self.timestamps:
            return max(0, int(seconds * self.fps))
        return max(0, min(len(self.timestamps) - 1,
                          bisect.bisect_right(self.timestamps, seconds + 1e-6) - 1))

    def window(self, start_seconds: float, end_seconds: float) -> tuple[int, int]:
        """Source-frame window, including the frame already visible at start."""
        if not self.timestamps:
            return max(0, int(start_seconds * self.fps)), max(0, math.ceil(end_seconds * self.fps))
        return self.frame_for_time(start_seconds), min(len(self.timestamps),
                                                       bisect.bisect_left(self.timestamps, end_seconds - 1e-9))

    def proxy_mapping(self, frame_start: int, frame_end: int, fps: float,
                      *, start_seconds: float | None = None, end_seconds: float | None = None) -> dict[str, Any]:
        """CFR proxy frame → actual source frame/PTS, preserving elapsed time."""
        start = self.timestamp(frame_start) if start_seconds is None else start_seconds
        end = self.timestamp(frame_end) if end_seconds is None else end_seconds
        count = max(0, round(max(0., end - start) * fps))
        frames = [min(frame_end - 1, max(frame_start, self.frame_for_time(start + i / fps)))
                  for i in range(count)]
        return {"schema_version": 1, "source_start_seconds": start,
                "source_end_seconds": end, "source_pts_origin": self.pts_origin,
                "source_timestamps_verified": self.verified, "proxy_fps": fps,
                "proxy_to_source_frame": frames,
                "proxy_to_source_seconds": [self.timestamp(frame) for frame in frames]}

    def to_dict(self) -> dict[str, Any]:
        return {"timestamps": list(self.timestamps), "duration": self.duration, "fps": self.fps,
                "pts_origin": self.pts_origin, "verified": self.verified}


@lru_cache(maxsize=8)
def _probe(path: str, size: int, modified_ns: int) -> VideoTimeline:
    del size, modified_ns
    check_control()
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_streams",
        "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time,duration_time:stream=avg_frame_rate,duration",
        "-of", "json", path,
    ], check=True, capture_output=True, text=True, timeout=60)
    check_control()
    data = json.loads(result.stdout)
    frames = data.get("frames", [])
    raw = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    if not raw or not all(math.isfinite(t) for t in raw) or any(b <= a for a, b in zip(raw, raw[1:])):
        raise ValueError("Video has no usable strictly ordered presentation timestamps")
    numerator, denominator = str(data["streams"][0]["avg_frame_rate"]).split("/")
    fps = float(numerator) / float(denominator)
    if fps <= 0 or not math.isfinite(fps):
        fps = 30.
    origin = raw[0]
    timestamps = tuple(t - origin for t in raw)
    # FFmpeg 7 reports duration_time; earlier versions use pkt_duration_time.
    # Average FPS cannot infer the duration of a VFR recording's final frame.
    observed_tail = frames[-1].get("duration_time") or frames[-1].get("pkt_duration_time")
    tail = float(observed_tail or (1 / fps))
    if not math.isfinite(tail) or tail <= 0:
        observed_tail = None
        tail = 1 / fps
    stream_duration = float(data["streams"][0].get("duration") or 0)
    if not math.isfinite(stream_duration) or stream_duration < timestamps[-1]:
        stream_duration = 0.
    frame_end = timestamps[-1] + tail
    if stream_duration and (not observed_tail or abs(frame_end - stream_duration) <= 1.001e-6):
        # Decimal PTS/duration precision must not propose a clip microscopically
        # beyond the API's authoritative source duration. Larger observed tails
        # are retained; an average-FPS fallback never extends a trusted stream.
        duration = stream_duration
    else:
        duration = max(frame_end, stream_duration)
    return VideoTimeline(timestamps, duration, fps, origin)


def point_interval(previous: dict[str, Any], current: dict[str, Any], fps: float) -> float:
    """Use observed elapsed PTS; FPS remains only a legacy-row fallback."""
    first, last = previous.get("timestamp_seconds"), current.get("timestamp_seconds")
    if first is not None and last is not None:
        elapsed = float(last) - float(first)
        return elapsed if math.isfinite(elapsed) else 0.
    return (int(current.get("frame_number", 0)) - int(previous.get("frame_number", 0))) / max(fps, 1.)


def seek_source_frame(cap: Any, frame_start: int, timeline: VideoTimeline) -> None:
    """Avoid OpenCV's average-FPS seeking approximation on actual VFR media."""
    import cv2

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0 if timeline.is_vfr else frame_start)
    if timeline.is_vfr:
        for _ in range(frame_start):
            check_control()
            if not cap.grab():
                raise RuntimeError("Source ended before the requested frame")
