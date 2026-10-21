"""Versioned media derivatives and measured presentation-duration checks.

Changing timing, source-frame mapping, or encoding behavior must bump the
generator version. A source hash alone cannot prove an old derivative is safe.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

MEDIA_GENERATOR_VERSION = "pts-proxy-v2"
OVERLAY_GENERATOR_VERSION = "pts-overlay-v4"
TIMELINE_REVISION = 2


def current_playback(media: dict[str, Any], source_sha256: str | None,
                     start: float, end: float) -> bool:
    """Only reuse media measured under the current generator contract."""
    return bool(
        source_sha256
        and media.get("generator_version") == MEDIA_GENERATOR_VERSION
        and media.get("timeline_revision") == TIMELINE_REVISION
        and media.get("source_sha256") == source_sha256
        and media.get("kind") == "clean_clip"
        and media.get("source_start_seconds") == start
        and media.get("source_end_seconds") == end
        and isinstance(media.get("duration_validation"), dict)
        and media["duration_validation"].get("passed") is True
        and media.get("encoder_config") == encoder_config()
        and isinstance(media.get("alignment_validation"), dict)
        and media["alignment_validation"].get("passed") is True
    )


def encoder_config(fps: float = 30, height: int = 540) -> dict[str, Any]:
    return {"codec": "h264", "pixel_format": "yuv420p", "fps": fps,
            "max_height": height, "crf": 23, "audio": "source_aac"}


def validate_alignment(path: Path, expected: dict[int, Any]) -> dict[str, Any]:
    """Check encoded first/middle/last pictures against the mapped source input."""
    import cv2
    import numpy as np

    capture = cv2.VideoCapture(str(path))
    samples = []
    try:
        for frame, reference in sorted(expected.items()):
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
            ok, picture = capture.read()
            if not ok:
                raise ValueError(f"Derivative alignment frame {frame} cannot be decoded")
            actual = cv2.resize(picture, (32, 18), interpolation=cv2.INTER_AREA)
            target = cv2.resize(reference, (32, 18), interpolation=cv2.INTER_AREA)
            error = float(np.abs(actual.astype(float) - target.astype(float)).mean())
            if error > 30:
                raise ValueError(f"Derivative source alignment failed at output frame {frame}")
            samples.append({"output_frame": frame, "mean_absolute_pixel_error": round(error, 3)})
    finally:
        capture.release()
    if not samples:
        raise ValueError("Derivative alignment has no mapped source samples")
    return {"passed": True, "method": "mapped_source_picture_comparison", "samples": samples}


def validate_duration(path: Path, expected_seconds: float, fps: float) -> dict[str, Any]:
    """Reject drifted or undecodable media before publishing its storage URI."""
    if not math.isfinite(expected_seconds) or expected_seconds <= 0 or fps <= 0:
        raise ValueError("Media duration and frame rate must be positive")
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=duration,codec_name,nb_frames", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("Encoded derivative contains no video stream")
    stream = streams[0]
    actual = float(stream.get("duration", "nan"))
    tolerance = 1.0 / fps + 0.001  # one output frame plus container rounding
    if (not math.isfinite(actual) or abs(actual - expected_seconds) > tolerance
            or stream.get("codec_name") != "h264"):
        raise ValueError(
            f"Encoded media failed timing validation: expected {expected_seconds:.6f}s, "
            f"measured {actual:.6f}s"
        )
    return {"passed": True, "expected_seconds": expected_seconds,
            "encoded_seconds": actual, "tolerance_seconds": tolerance,
            "codec": stream["codec_name"]}


def preserve_audio(source: Path, encoded: Path, start: float, end: float,
                   *, video_pts_origin: float = 0) -> dict[str, Any]:
    """Copy the matching source audio window into the clean clip as AAC."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name:format=start_time", "-of", "json", str(source)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    source_metadata = json.loads(probe.stdout)
    if not source_metadata.get("streams"):
        return {"status": "absent_in_source"}
    from worker.media_process import run_checked

    destination = encoded.with_name(encoded.stem + ".audio.mp4")
    source_origin = float((source_metadata.get("format") or {}).get("start_time") or 0)
    seek_seconds = max(0, start + video_pts_origin - source_origin)
    run_checked([
        "ffmpeg", "-v", "error", "-y", "-i", str(encoded),
        "-ss", f"{seek_seconds:.9f}", "-i", str(source),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
        "-t", f"{end - start:.9f}", "-movflags", "+faststart", str(destination),
    ])
    validation = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,duration",
         "-of", "json", str(destination)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    streams = json.loads(validation.stdout).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise ValueError("Source audio could not be preserved")
    drift = abs(float(audio.get("start_time", 0)) - float(video.get("start_time", 0)))
    tail_drift = abs(float(audio["duration"]) - float(video["duration"]))
    if not math.isfinite(drift + tail_drift) or max(drift, tail_drift) > 0.1:
        raise ValueError("Derivative audio exceeds the 100 ms synchronization tolerance")
    destination.replace(encoded)
    return {"status": "preserved", "codec": "aac", "start_drift_seconds": drift,
            "duration_drift_seconds": tail_drift, "source_seek_seconds": seek_seconds,
            "passed": True}
