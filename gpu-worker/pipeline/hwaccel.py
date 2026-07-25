"""NVIDIA hardware-acceleration helpers for video decode (NVDEC) and encode (NVENC).

Provides thin wrappers that probe for hardware support once and cache the result.
All helpers fall back to CPU paths transparently so callers never need to handle
the GPU-unavailable case.

Usage::

    from pipeline.hwaccel import nvdec_video_capture, nvenc_ffmpeg_codec_args

    # Decode — drop-in replacement for cv2.VideoCapture(path)
    cap = nvdec_video_capture(path)

    # Encode — returns ["-c:v", "h264_nvenc", ...] or ["-c:v", "libx264", ...]
    codec_args = nvenc_ffmpeg_codec_args()

Environment variables:
    DISABLE_NVDEC — set to "1" to force CPU decode even when NVDEC is available.
    DISABLE_NVENC — set to "1" to force CPU encode even when NVENC is available.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

import cv2
import structlog

log = structlog.get_logger(__name__)


@functools.lru_cache(maxsize=1)
def probe_nvdec() -> bool:
    """Return True if NVDEC is available on this system.

    Checks:
      1. ``DISABLE_NVDEC`` env var is not set.
      2. ``nvidia-smi`` is reachable (driver loaded).
      3. OpenCV was built with FFMPEG backend (required for hwaccel passthrough).
    """
    if os.environ.get("DISABLE_NVDEC", "") == "1":
        log.info("nvdec_disabled_by_env")
        return False
    try:
        subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        log.info("nvdec_unavailable_no_gpu")
        return False

    try:
        build_info = cv2.getBuildInformation()
    except Exception:
        log.warning("nvdec_unavailable_opencv_build_info_error")
        return False

    ffmpeg_enabled = False
    for line in build_info.splitlines():
        if "ffmpeg" in line.lower() and "yes" in line.lower():
            ffmpeg_enabled = True
            break

    if not ffmpeg_enabled:
        log.info("nvdec_unavailable_no_opencv_ffmpeg")
        return False

    log.info("nvdec_available")
    return True


@functools.lru_cache(maxsize=1)
def probe_nvenc() -> bool:
    """Return True if NVENC actually works at runtime.

    Checking ``ffmpeg -encoders`` alone is not enough: distro builds compile
    the ``h264_nvenc`` encoder in, but on a GPU-less host it fails at open
    time ("Cannot load libcuda.so.1"). So after the cheap listing check, run
    a tiny null encode to prove the driver stack is really usable.
    """
    if os.environ.get("DISABLE_NVENC", "") == "1":
        log.info("nvenc_disabled_by_env")
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "h264_nvenc" not in result.stdout:
            log.info("nvenc_unavailable")
            return False
        smoke = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=20,
        )
        if smoke.returncode == 0:
            log.info("nvenc_available")
            return True
        log.info("nvenc_listed_but_unusable")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    log.info("nvenc_unavailable")
    return False


def nvdec_video_capture(path: str | Path) -> cv2.VideoCapture:
    """Return a ``cv2.VideoCapture`` with NVDEC if available, else CPU.

    On NVDEC-capable systems this sets ``CAP_PROP_HW_ACCELERATION`` to
    ``VIDEO_ACCELERATION_ANY`` which lets the FFmpeg backend negotiate
    CUVID/NVDEC automatically.
    """
    str_path = str(path)
    if probe_nvdec():
        cap = cv2.VideoCapture(str_path, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        if cap.isOpened():
            log.debug("nvdec_capture_opened", path=str_path)
            return cap
        log.warning("nvdec_capture_fallback", path=str_path)
        cap.release()

    cap = cv2.VideoCapture(str_path)
    return cap


def nvenc_ffmpeg_codec_args(bitrate: str = "2000k") -> list[str]:
    """Return ffmpeg video-codec arguments for NVENC or libx264 fallback.

    Returns a list like ``["-c:v", "h264_nvenc", "-b:v", "2000k"]``.
    """
    if probe_nvenc():
        return ["-c:v", "h264_nvenc", "-b:v", bitrate]
    return ["-c:v", "libx264", "-b:v", bitrate]
