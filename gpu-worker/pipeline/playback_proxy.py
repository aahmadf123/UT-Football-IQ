"""Clean H264 play media, preserving original presentation times and frames."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import cv2

from pipeline import backend, storage
from pipeline.runtime import check_control, configure_cpu_threads
from pipeline.video_timeline import VideoTimeline
from worker.media_process import run_checked


class PlaybackProxySession:
    """Read sorted play windows sequentially, avoiding whole-film decode per cut."""

    def __init__(
        self, source: Path, timeline: VideoTimeline, *, source_sha256: str | None = None
    ) -> None:
        self.source = source
        self.timeline = timeline
        self.source_sha256 = source_sha256
        self.capture: Any = None
        self.frame_number = 0

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def _position(self, frame_number: int) -> None:
        if self.capture is None or self.frame_number > frame_number:
            self.close()
            self.capture = cv2.VideoCapture(str(self.source))
            self.frame_number = 0
            if not self.capture.isOpened():
                raise ValueError("Unable to decode recording for clean playback")
        # OpenCV frame seeks use average FPS on several VFR codecs. Reading
        # actual frames guarantees alignment with ffprobe and detector indices.
        while self.frame_number < frame_number:
            check_control()
            ok = self.capture.grab()
            if not ok:
                raise ValueError("Recording ended before the requested play")
            self.frame_number += 1

    def run(
        self,
        clip_id: str,
        start: float,
        end: float,
        *,
        fps: float = 30,
        height: int = 540,
    ) -> dict[str, Any]:
        if start < 0 or end <= start:
            raise ValueError("Playback window must have positive duration")
        frame_start, frame_end = self.timeline.window(start, end)
        mapping = self.timeline.proxy_mapping(
            frame_start, frame_end, fps, start_seconds=start, end_seconds=end
        )
        if not mapping["proxy_to_source_frame"]:
            raise ValueError("Playback window contains no source frames")
        self._position(frame_start)
        repetitions = Counter(mapping["proxy_to_source_frame"])
        width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        scale = min(1, height / max(1, source_height))
        size = (max(2, int(width * scale) // 2 * 2), max(2, int(source_height * scale) // 2 * 2))
        with tempfile.TemporaryDirectory(prefix="football-iq-playback-") as directory:
            raw_path = Path(directory) / "raw.mp4"
            output = Path(directory) / "clip.mp4"
            writer = cv2.VideoWriter(str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
            if not writer.isOpened():
                raise ValueError("Unable to prepare clean play media")
            try:
                while self.frame_number < frame_end:
                    check_control()
                    ok, frame = self.capture.read()
                    if not ok:
                        raise ValueError("Recording ended inside the requested play")
                    count = repetitions.get(self.frame_number, 0)
                    if count:
                        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                        for _ in range(count):
                            writer.write(frame)
                    self.frame_number += 1
            finally:
                writer.release()
            check_control()
            run_checked(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(raw_path),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "23",
                    "-threads",
                    str(configure_cpu_threads()),
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-an",
                    str(output),
                ]
            )
            playback_uri = storage.upload_file(
                output, f"playback_proxies/{clip_id}/clip.mp4", "video/mp4", bucket="clips"
            )
        timeline_uri = storage.upload_bytes(
            json.dumps(mapping).encode(),
            f"playback_proxies/{clip_id}/timeline.json",
            "application/json",
            bucket="artifacts",
        )
        media = {
            "schema_version": 1,
            "source_sha256": self.source_sha256,
            "kind": "clean_clip",
            "source_start_seconds": start,
            "source_end_seconds": end,
            "proxy_fps": fps,
            "timeline_uri": timeline_uri,
            "source_timestamps_verified": self.timeline.verified,
        }
        backend.patch_clip_storage_uri(clip_id, playback_uri, playback=media)
        return {"playback_uri": playback_uri, "playback": media, "proxy_timeline": mapping}
