"""Clean H264 play media, preserving original presentation times and frames."""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import cv2

from pipeline import backend, storage
from pipeline.runtime import check_control, configure_cpu_threads
from pipeline.video_timeline import VideoTimeline, iter_frames


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
            # A keyframe every second: the x264 default (250 frames, ~8 s at
            # 30 fps) made scrubbing and frame-stepping in review wait.
            "-g",
            str(max(1, round(fps))),
            "-keyint_min",
            str(max(1, round(fps))),
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
        # Frames reach ffmpeg through a writer thread: a blocking pipe write
        # can't be interrupted, and select() on pipes is Unix-only (the GPU
        # worker runs on Windows). The caller only ever waits on this queue,
        # in short slices that check cancel, the deadline and ffmpeg's exit.
        self._frames: queue.Queue[bytes | None] = queue.Queue(maxsize=4)
        self._writer: threading.Thread | None = None
        self._writer_error: BaseException | None = None

    def __enter__(self) -> _StreamingEncoder:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=self.errors,
            bufsize=0,
        )
        assert self.process.stdin is not None
        self.deadline = time.monotonic() + self._timeout
        self._writer = threading.Thread(target=self._pump, name="proxy-encoder", daemon=True)
        self._writer.start()
        return self

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdin is not None
        stdin = self.process.stdin
        try:
            while (chunk := self._frames.get()) is not None:
                stdin.write(chunk)
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._writer_error = exc
        finally:
            try:
                stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def write(self, frame: Any) -> None:
        assert self.process is not None
        data = frame.tobytes()
        while True:
            self._check_control()
            if self._writer_error is not None or self.process.poll() is not None:
                self._fail()
            try:
                self._frames.put(data, timeout=min(self._poll_interval, self._remaining()))
                return
            except queue.Full:
                continue

    def _finish_writing(self) -> None:
        """Send end-of-stream and wait for the writer to drain, interruptibly."""
        if self._writer is None:
            return
        while True:
            self._check_control()
            try:
                self._frames.put(None, timeout=min(self._poll_interval, self._remaining()))
                break
            except queue.Full:
                if self.process is not None and self.process.poll() is not None:
                    return
        while self._writer.is_alive():
            self._check_control()
            self._writer.join(timeout=min(self._poll_interval, self._remaining()))

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
            if exc_type is not None:
                self._terminate()
                return
            self._finish_writing()
            if self._writer_error is not None:
                self._fail()
            self._wait_for_exit()
            if self.process.returncode:
                self._fail()
        finally:
            if self.process.poll() is None:
                self._terminate()
            if self._writer is not None and self._writer.is_alive():
                # ffmpeg termination unblocks writes; the sentinel unblocks queue.get().
                while True:
                    try:
                        self._frames.get_nowait()
                    except queue.Empty:
                        break
                self._frames.put_nowait(None)
                self._writer.join(timeout=5)
            if self.process.stdin is not None and not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
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

    def close(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None

    def _open(self) -> Any:
        if self.capture is None:
            self.capture = cv2.VideoCapture(str(self.source))
            if not self.capture.isOpened():
                self.capture = None
                raise ValueError("Unable to decode recording for clean playback")
        return self.capture

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
        capture = self._open()
        repetitions = Counter(mapping["proxy_to_source_frame"])
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        scale = min(1, height / max(1, source_height))
        size = (max(2, int(width * scale) // 2 * 2), max(2, int(source_height * scale) // 2 * 2))
        with tempfile.TemporaryDirectory(prefix="football-iq-playback-") as directory:
            output = Path(directory) / "clip.mp4"
            # Frames go straight into one x264 encode. (Writing an OpenCV
            # MPEG-4 file first and re-encoding it took twice as long and,
            # at ultrafast, made files ~4x larger to upload.)
            with _StreamingEncoder(output, size, fps) as encoder:
                # Frames found by timestamp (never by counting, which drifts
                # where the decoder drops frames). Frames the proxy drops
                # (60 -> 30 fps) are only decoded, never converted to BGR;
                # an undecodable frame shows the next good picture.
                written = 0
                previous: Any = None
                small: Any = None
                for index, frame in iter_frames(
                    capture,
                    self.timeline,
                    frame_start,
                    frame_end,
                    wanted=lambda i: repetitions.get(i, 0) > 0,
                ):
                    if frame is not previous:
                        small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                        previous = frame
                    count = repetitions[index]
                    for _copy in range(count):
                        encoder.write(small)
                    written += count
                if written == 0:
                    raise ValueError("Recording ended before the requested play")
                # The recording ended inside the play: hold the last picture.
                for _copy in range(len(mapping["proxy_to_source_frame"]) - written):
                    encoder.write(small)
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
