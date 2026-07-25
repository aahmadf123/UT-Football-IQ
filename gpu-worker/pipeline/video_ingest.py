"""Video ingestion abstraction for the pose-lite biomechanics pipeline.

Provides a clean `VideoSource` protocol so downstream stages never reference file
paths directly.  This is the DJI drone .mp4 readiness layer — when real footage is
available, replace `MockVideoSource` with `LocalFileVideoSource` or `R2VideoSource`.

Supported sources:
    LocalFileVideoSource  — any OpenCV-readable video file (mp4, avi, mov, …).
                            Extracts DJI drone metadata (GPS, altitude) via ffprobe
                            when available; gracefully omits metadata if absent.
    R2VideoSource         — downloads from Cloudflare R2 to a temp file then delegates
                            to LocalFileVideoSource.  Cleans up on exit.
    MockVideoSource       — deterministic synthetic frames for unit tests and CI.
                            Never requires real video footage.

DJI drone recordings (4 K H.264/HEVC, variable FPS) are handled transparently by
OpenCV's VideoCapture.  The ffprobe metadata extractor captures DJI XMP tags if
present in the container.

Usage::

    source = LocalFileVideoSource("clips/TOL_20260901_P1_S1_001_drone.mp4")
    for frame_num, frame in source.iter_frames(stride=3):
        keypoints = estimator.estimate(frame)

    # In tests
    source = MockVideoSource(fps=30.0, total_frames=90)
    for frame_num, frame in source.iter_frames():
        ...
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import structlog

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

# COCO 17-keypoint blank frame shape used by MockVideoSource
_MOCK_FRAME_H = 720
_MOCK_FRAME_W = 1280
_MOCK_FRAME_C = 3


# ── Protocol ──────────────────────────────────────────────────────────────────


class VideoSource:
    """Base class / informal protocol for all video sources.

    Subclasses must implement `iter_frames` and expose `fps` and `total_frames`.
    """

    fps: float
    total_frames: int
    metadata: dict[str, Any]

    def iter_frames(self, stride: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        """Yield (frame_number, BGR numpy array) pairs.

        Args:
            stride: Yield every `stride`-th frame (1 = every frame).
        """
        raise NotImplementedError


# ── Local file source ─────────────────────────────────────────────────────────


class LocalFileVideoSource(VideoSource):
    """Read any OpenCV-supported video file from the local filesystem.

    Designed for DJI drone .mp4 recordings (4 K, 30/60 FPS, H.264/HEVC).
    Attempts to read DJI XMP/GPS metadata via ffprobe; skips gracefully if
    ffprobe is absent or the file has no metadata stream.

    Args:
        path: Path to the video file.  Accepts str or pathlib.Path.
    """

    def __init__(self, path: str | Path) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise ImportError("opencv-python-headless is required for LocalFileVideoSource") from exc

        self._cv2 = cv2
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"Video file not found: {self._path}")

        from pipeline.hwaccel import nvdec_video_capture

        cap = nvdec_video_capture(self._path)
        if not cap.isOpened():
            raise OSError(f"OpenCV could not open: {self._path}")

        self.fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()

        self.metadata = self._extract_metadata()
        log.info(
            "video_source_opened",
            path=str(self._path),
            fps=self.fps,
            total_frames=self.total_frames,
            has_dji_meta=bool(self.metadata.get("dji")),
        )

    def _extract_metadata(self) -> dict[str, Any]:
        """Extract container metadata via ffprobe (DJI GPS, altitude, etc.)."""
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "quiet",
                    "-print_format", "json",
                    "-show_format",
                    str(self._path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout)
            fmt = data.get("format", {})
            tags = fmt.get("tags", {})
            meta: dict[str, Any] = {
                "duration_seconds": float(fmt.get("duration", 0) or 0),
                "size_bytes": int(fmt.get("size", 0) or 0),
                "bit_rate": int(fmt.get("bit_rate", 0) or 0),
            }
            # DJI stores GPS in XMP tags embedded in the MP4 container
            dji_tags = {k: v for k, v in tags.items() if k.lower().startswith("com.dji")}
            if dji_tags:
                meta["dji"] = dji_tags
            return meta
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            return {}

    def iter_frames(self, stride: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        """Yield (frame_number, BGR frame) for every `stride`-th frame."""
        from pipeline.hwaccel import nvdec_video_capture

        cap = nvdec_video_capture(self._path)
        if not cap.isOpened():
            log.error("video_source_open_failed", path=str(self._path))
            return
        try:
            frame_num = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_num % stride == 0:
                    yield frame_num, frame
                frame_num += 1
        finally:
            cap.release()


# ── R2 source ─────────────────────────────────────────────────────────────────


class R2VideoSource(VideoSource):
    """Download a video from Cloudflare R2 and read it via LocalFileVideoSource.

    The temp file is cleaned up when the context manager exits or when the
    object is garbage-collected.

    Args:
        r2_key: R2 object key (e.g. ``raw-video/TOL_20260901_P1.mp4``).
    """

    def __init__(self, r2_key: str) -> None:
        from pipeline import r2 as r2_mod

        self._r2_key = r2_key
        self._temp_path: Path | None = None
        self._inner: LocalFileVideoSource | None = None
        self._download(r2_mod)

    def _download(self, r2_mod: Any) -> None:
        self._temp_path = r2_mod.download_to_temp(self._r2_key)
        self._inner = LocalFileVideoSource(self._temp_path)
        self.fps = self._inner.fps
        self.total_frames = self._inner.total_frames
        self.metadata = self._inner.metadata

    def iter_frames(self, stride: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        if self._inner is None:
            return
        yield from self._inner.iter_frames(stride=stride)

    def cleanup(self) -> None:
        if self._temp_path and self._temp_path.exists():
            self._temp_path.unlink(missing_ok=True)
            log.debug("r2_temp_cleaned", path=str(self._temp_path))

    def __del__(self) -> None:
        self.cleanup()


# ── Mock source (for tests and CI) ────────────────────────────────────────────

_RNG_SEED_DEFAULT = 42


class MockVideoSource(VideoSource):
    """Deterministic synthetic video source for tests and CI.

    No video file is required.  Frames are generated as random-but-reproducible
    numpy arrays so downstream logic can be exercised without real footage.

    Args:
        fps:           Frames per second (default 30.0).
        total_frames:  Number of frames to yield (default 90 = 3 s at 30 FPS).
        seed:          NumPy RNG seed for determinism (default 42).
        frames:        Optional explicit list of ``(frame_num, ndarray)`` pairs.
                       When provided, ``total_frames`` is inferred from the list.
    """

    def __init__(
        self,
        fps: float = 30.0,
        total_frames: int = 90,
        seed: int = _RNG_SEED_DEFAULT,
        frames: list[tuple[int, np.ndarray]] | None = None,
    ) -> None:
        self.fps = fps
        self.metadata: dict[str, Any] = {"source": "mock"}
        self._seed = seed

        if frames is not None:
            self._frames = frames
            self.total_frames = len(frames)
        else:
            self.total_frames = total_frames
            rng = np.random.default_rng(seed)
            self._frames = [
                (i, rng.integers(0, 256, (_MOCK_FRAME_H, _MOCK_FRAME_W, _MOCK_FRAME_C),
                                  dtype=np.uint8))
                for i in range(total_frames)
            ]

    def iter_frames(self, stride: int = 1) -> Iterator[tuple[int, np.ndarray]]:
        for frame_num, frame in self._frames:
            if frame_num % stride == 0:
                yield frame_num, frame


# ── Convenience context manager ───────────────────────────────────────────────


@contextmanager
def open_video(uri: str | Path | None, *, fps_fallback: float = 30.0) -> Iterator[VideoSource]:
    """Context manager that returns the right VideoSource for a given URI.

    - ``None`` or empty string → ``MockVideoSource`` (no-op, safe for tests)
    - ``r2://…``               → ``R2VideoSource`` (downloads from R2, cleans up)
    - ``local://…``/``file://…`` → ``LocalFileVideoSource`` on the resolved path
    - Anything else            → ``LocalFileVideoSource`` (local .mp4 path)

    Example::

        with open_video(input_uri) as source:
            for fn, frame in source.iter_frames(stride=3):
                ...
    """
    if not uri:
        yield MockVideoSource(fps=fps_fallback)
        return

    uri_str = str(uri)
    if uri_str.startswith("r2://"):
        # Pass the full URI through — the storage facade parses the bucket,
        # so multi-bucket references are honoured (a bare key would silently
        # fall back to the single default bucket).
        source = R2VideoSource(uri_str)
        try:
            yield source
        finally:
            source.cleanup()
    elif uri_str.startswith(("local://", "file://")):
        from pipeline import storage as storage_mod

        path = storage_mod.resolve_readable_path(uri_str)
        if path is None:
            raise FileNotFoundError(f"Unresolvable storage reference: {uri_str}")
        yield LocalFileVideoSource(path)
    else:
        yield LocalFileVideoSource(uri_str)
