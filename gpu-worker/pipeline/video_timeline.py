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
            # The caller's FPS may be a placeholder (the orchestrator's 30.0
            # before ingest runs); the container's own rate wins. Trusting 30
            # on 59.94 film doubled every timestamp and cut each play from
            # the wrong part of the game.
            fps, frame_count = _container_rate(source)
            fps = fps or (fps_fallback if fps_fallback > 0 else 30.)
            frame_count = frame_count or frame_count_fallback
            return cls(tuple(i / fps for i in range(frame_count)),
                       frame_count / fps, fps, verified=False)

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


def _container_rate(source: Path) -> tuple[float, int]:
    """Average FPS and frame count from the container, or zeros."""
    try:
        import cv2

        capture = cv2.VideoCapture(str(source))
        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        finally:
            capture.release()
    except Exception:  # noqa: BLE001 - metadata only; the caller has a fallback
        return 0., 0
    return (fps if math.isfinite(fps) and fps > 0 else 0.), max(0, count)


#: Above this many frames, decoding each one to read its timestamp outlasts
#: any sensible timeout (a 90-minute 60 fps game is ~300k frames), so
#: presentation times come from the container's packet index instead.
FRAME_PROBE_LIMIT = 20_000


def _ffprobe_json(path: str, *args: str, timeout: float) -> dict[str, Any]:
    check_control()
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *args, "-of", "json", path],
                            check=True, capture_output=True, text=True, timeout=timeout)
    check_control()
    return json.loads(result.stdout)


@lru_cache(maxsize=8)
def _probe(path: str, size: int, modified_ns: int) -> VideoTimeline:
    del size, modified_ns
    stream = _ffprobe_json(path, "-show_entries", "stream=avg_frame_rate,duration,nb_frames",
                           timeout=60)["streams"][0]
    numerator, denominator = str(stream["avg_frame_rate"]).split("/")
    fps = float(numerator) / float(denominator)
    if fps <= 0 or not math.isfinite(fps):
        fps = 30.
    stream_duration = float(stream.get("duration") or 0)
    expected_frames = int(stream.get("nb_frames") or 0) or round(stream_duration * fps)

    observed_tail: str | None
    if expected_frames <= 0 or expected_frames > FRAME_PROBE_LIMIT:
        # Demux only, no decode: seconds even for a full game. Packets come
        # in decode order; B-frames reorder for presentation. A packet's
        # duration follows decode order, so the final frame's is unknown and
        # the average frame interval stands in (one frame on a long film).
        packets = _ffprobe_json(path, "-show_entries", "packet=pts_time", timeout=600).get("packets", [])
        if any(p.get("pts_time") in (None, "N/A") for p in packets):
            raise ValueError("Video has packets without usable presentation timestamps")
        raw = sorted(float(p["pts_time"]) for p in packets)
        observed_tail = None
    else:
        frames = _ffprobe_json(
            path, "-show_frames", "-show_entries",
            "frame=best_effort_timestamp_time,pkt_duration_time,duration_time", timeout=120,
        ).get("frames", [])
        raw = [float(frame["best_effort_timestamp_time"]) for frame in frames]
        # FFmpeg 7 reports duration_time; earlier versions use pkt_duration_time.
        # Average FPS cannot infer the duration of a VFR recording's final frame.
        observed_tail = (frames[-1].get("duration_time") or frames[-1].get("pkt_duration_time")) if frames else None
    if not raw or not all(math.isfinite(t) for t in raw) or any(b <= a for a, b in zip(raw, raw[1:])):
        raise ValueError("Video has no usable strictly ordered presentation timestamps")
    origin = raw[0]
    timestamps = tuple(t - origin for t in raw)
    tail = float(observed_tail or (1 / fps))
    if not math.isfinite(tail) or tail <= 0:
        observed_tail = None
        tail = 1 / fps
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


# ── Reading frames by their place on the timeline ────────────────────────────
#
# Frame indices everywhere in the pipeline are positions on the PTS timeline
# (one per packet, from ffprobe). Counting decoded frames is NOT the same
# thing: decoders silently drop frames they cannot reconstruct. The Michigan
# State recording loses ~110 frames in its first 400 s, so by 25 minutes in a
# counted index ran 410 frames (6.8 s) behind the picture -- every box from
# detection, every play video and every calibration drifted with it.
# These helpers identify each decoded frame by its own timestamp instead.

#: How far before a target to land a seek, so decoding forward reaches it.
SEEK_MARGIN_S = 2.0


def current_index(cap: Any, timeline: VideoTimeline) -> int:
    """Timeline index of the frame the capture last grabbed, from its PTS."""
    import cv2

    seconds = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
    # Millisecond rounding must not drop a frame onto its predecessor.
    return timeline.frame_for_time(seconds + 0.25 / max(timeline.fps, 1.0))


def seek_to(cap: Any, timeline: VideoTimeline, frame: int) -> int | None:
    """Grab up to ``frame`` and return the index actually grabbed.

    That is ``frame`` itself, or the first decodable frame after it when
    ``frame`` cannot be decoded; ``None`` past the end. The caller reads the
    picture with ``cap.retrieve()``.
    """
    import cv2

    target = timeline.timestamp(frame) if frame <= len(timeline.timestamps) else frame / timeline.fps
    index: int | None = None
    margin = SEEK_MARGIN_S
    for _ in range(4):
        check_control()
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, target - margin) * 1000.0)
        if not cap.grab():
            return None
        index = current_index(cap, timeline)
        if index <= frame or target - margin <= 0:
            break
        margin *= 4  # landed past the target: back off further
    steps = 0
    while index is not None and index < frame:
        check_control()
        if not cap.grab():
            return None
        following = current_index(cap, timeline)
        steps += 1
        if following <= index and steps > 4 * max(timeline.fps, 1.0) * (SEEK_MARGIN_S + 1):
            return None  # the capture is not advancing in time
        index = max(following, index + 1) if following <= index else following
    return index


def iter_frames(
    cap: Any,
    timeline: VideoTimeline,
    start: int = 0,
    end: int | None = None,
    *,
    wanted: Any = None,
    fill_gaps: bool = True,
) -> Any:
    """Yield ``(index, frame)`` in timeline order from ``start`` to ``end``.

    ``wanted(index)`` limits which frames are retrieved; the rest are only
    decoded. With ``fill_gaps`` an index the decoder could not produce gets
    the next good picture, so a rendered video keeps the timeline's length;
    without it (detection, sampling) missing frames are simply absent.
    """
    index = seek_to(cap, timeline, start)
    expected = start
    # A capture that never reports its end must not run on forever.
    if end is None and timeline.timestamps:
        end = len(timeline.timestamps)
    while index is not None and (end is None or expected < end):
        check_control()
        last = index if end is None else min(index, end - 1)
        span = range(expected, last + 1) if fill_gaps else ([index] if index <= last else [])
        need = [i for i in span if wanted is None or wanted(i)]
        if need:
            ok, frame = cap.retrieve()
            if ok and frame is not None:
                for i in need:
                    yield i, frame
        expected = index + 1
        if end is not None and index >= end - 1:
            return
        if not cap.grab():
            return
        following = current_index(cap, timeline)
        # Equal timestamps (rare muxing glitches) still advance one place.
        index = following if following > index else index + 1


def read_frames(cap: Any, timeline: VideoTimeline, indices: Any) -> dict[int, Any]:
    """Exactly these frames, keyed by index; undecodable ones are left out.

    Nearby targets are reached by decoding forward, distant ones by seeking.
    """
    frames: dict[int, Any] = {}
    index: int | None = None
    for target in sorted(set(int(i) for i in indices)):
        check_control()
        far = index is None or target < index or (
            timeline.timestamp(min(target, len(timeline.timestamps)))
            - timeline.timestamp(min(index, len(timeline.timestamps)))
            > 2 * SEEK_MARGIN_S
        )
        if far:
            index = seek_to(cap, timeline, target)
        while index is not None and index < target:
            if not cap.grab():
                index = None
                break
            index = current_index(cap, timeline)
        if index is None:
            break
        if index == target:
            ok, frame = cap.retrieve()
            if ok and frame is not None:
                frames[target] = frame
    return frames


def seek_source_frame(cap: Any, frame_start: int, timeline: VideoTimeline) -> None:
    """Deprecated: counts frames, so it drifts on recordings with undecodable
    frames. Use :func:`seek_to` / :func:`iter_frames` / :func:`read_frames`."""
    import cv2

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0 if timeline.is_vfr else frame_start)
    if timeline.is_vfr:
        for _ in range(frame_start):
            check_control()
            if not cap.grab():
                raise RuntimeError("Source ended before the requested frame")
