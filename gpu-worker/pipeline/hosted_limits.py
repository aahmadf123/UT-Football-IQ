"""Scratch-space bounds for the CPU process sharing an 8 GB API container."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

GIB = 1024**3
MAX_SOURCE_BYTES = 512 * 1024**2
MAX_DURATION_SECONDS = 300
MAX_PIXELS = 1920 * 1080
MIN_SCRATCH_FREE_BYTES = 4 * GIB


def processing_limits() -> dict[str, Any]:
    return {
        "source_max_bytes": MAX_SOURCE_BYTES,
        "duration_max_seconds": MAX_DURATION_SECONDS,
        "max_pixels": MAX_PIXELS,
        "min_scratch_free_bytes": MIN_SCRATCH_FREE_BYTES,
        "message": (
            "Shared hosted CPU processing supports recordings up to 512 MiB, "
            "5 minutes and 1080p, with 4 GiB of free temporary space. "
            "Upload a short processing copy for larger film; originals stay in the shared library."
        ),
    }


class HostedCapacityError(RuntimeError):
    """Original film remains available; this hosted worker cannot process it."""


def source_limit_reason(size: Any, free_bytes: int) -> str | None:
    if not isinstance(size, int) or size <= 0:
        return "Recording size is unverified. Re-register the original recording before hosted processing."
    if size > MAX_SOURCE_BYTES:
        return "Hosted CPU processing accepts up to 512 MiB per recording. Upload a short segment for processing; the original stays in the shared library."
    if free_bytes < max(MIN_SCRATCH_FREE_BYTES, size * 6 + GIB):
        return "Hosted processing has insufficient temporary disk space. Retry after active processing completes."
    return None


def check_source(size: Any) -> str | None:
    return source_limit_reason(size, shutil.disk_usage(tempfile.gettempdir()).free)


def release_completed_checkpoints(root: Path) -> None:
    """Reclaim a succeeded recording's resume ledger on the shared hosted disk.

    Only called once the job's results are published; paused, failed and
    running jobs keep their ledgers so a retry can resume from them.
    """
    if os.environ.get("PIPELINE_HOSTED") != "1":
        return
    shutil.rmtree(root, ignore_errors=True)


def validate_master(path: Path) -> None:
    """Probe actual media after its bounded download, before stage copies/render."""
    if os.environ.get("PIPELINE_HOSTED") != "1":
        return
    reason = check_source(path.stat().st_size)
    if reason:
        raise HostedCapacityError(reason)
    probe = subprocess.run(  # noqa: S603,S607 — fixed executable/argv, no shell
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    metadata = json.loads(probe.stdout)
    duration = float(metadata.get("format", {}).get("duration", 0))
    stream = (metadata.get("streams") or [{}])[0]
    pixels = int(stream.get("width", 0)) * int(stream.get("height", 0))
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION_SECONDS:
        raise HostedCapacityError(
            "Hosted CPU processing accepts recordings up to 5 minutes. Upload a short segment; the original stays in the shared library."
        )
    if not 0 < pixels <= MAX_PIXELS:
        raise HostedCapacityError(
            "Hosted CPU processing accepts up to 1080p video. Upload a smaller processing copy; the original stays in the shared library."
        )
