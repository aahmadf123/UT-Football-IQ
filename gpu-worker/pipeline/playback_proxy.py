"""Clean H264 play media, preserving original presentation times and frames."""

from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import cv2

from pipeline import backend, storage
from pipeline.runtime import check_control, configure_cpu_threads
from pipeline.video_timeline import VideoTimeline


class _StreamingEncoder:
    """Raw BGR frames on stdin -> browser-ready H.264 MP4, in one ffmpeg pass."""

    _timeout = 180
    _poll_interval = 0.25

    def __init__(self, output: Path, size: tuple[int, int], fps: float) -> None:
        self.command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{size[0]}x{size[1]}",
            "-r",
            f"{fps:g}",
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
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
        self.errors = tempfile.TemporaryFile()
        self.process: subprocess.Popen[bytes] | None = None
        self.deadline: float | None = None

    def __enter__(self) -> _StreamingEncoder:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self.errors,
            bufsize=0,
        )
        assert self.process.stdin is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        self.deadline = time.monotonic() + self._timeout
        return self

    def write(self, frame: Any) -> None:
        assert self.process is not None and self.process.stdin is not None
        descriptor = self.process.stdin.fileno()
        remaining = memoryview(frame.tobytes())
        while remaining:
            self._check_control()
            if self.process.poll() is not None:
                self._fail()
            _, writable, _ = select.select(
                [],
                [descriptor],
                [],
                min(self._poll_interval, self._remaining()),
            )
            if not writable:
                continue
            try:
                written = os.write(descriptor, remaining)
            except BlockingIOError:
                continue
            except BrokenPipeError:
                self._fail()
            if not written:
                self._fail()
            remaining = remaining[written:]

    def _fail(self) -> None:
        assert self.process is not None
        self._wait_for_exit()
        self.errors.seek(0)
        raise subprocess.CalledProcessError(
            self.process.returncode or 1, self.command, stderr=self.errors.read(16 * 1024)
        )

    def _remaining(self) -> float:
        assert self.deadline is not None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.command, self._timeout)
        return remaining

    def _check_control(self) -> None:
        check_control()
        self._remaining()

    def _wait_for_exit(self) -> None:
        assert self.process is not None
        while self.process.poll() is None:
            self._check_control()
            try:
                self.process.wait(timeout=min(self._poll_interval, self._remaining()))
            except subprocess.TimeoutExpired:
                continue
        self._check_control()

    def _terminate(self) -> None:
        assert self.process is not None
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self.process is not None
        try:
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            if exc_type is not None:
                self._terminate()
                return
            self._wait_for_exit()
            if self.process.returncode:
                self._fail()
        finally:
            if self.process.poll() is None:
                self._terminate()
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            self.errors.close()


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
            output = Path(directory) / "clip.mp4"
            # Frames go straight into one x264 encode. (Writing an OpenCV
            # MPEG-4 file first and re-encoding it took twice as long and,
            # at ultrafast, made files ~4x larger to upload.)
            with _StreamingEncoder(output, size, fps) as encoder:
                while self.frame_number < frame_end:
                    check_control()
                    count = repetitions.get(self.frame_number, 0)
                    # Frames the proxy drops (60 -> 30 fps) are only decoded,
                    # never converted to BGR.
                    if not count:
                        if not self.capture.grab():
                            raise ValueError("Recording ended inside the requested play")
                        self.frame_number += 1
                        continue
                    ok, frame = self.capture.read()
                    if not ok:
                        raise ValueError("Recording ended inside the requested play")
                    frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                    for _ in range(count):
                        encoder.write(frame)
                    self.frame_number += 1
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
