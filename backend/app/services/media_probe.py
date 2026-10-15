"""Bounded, local-only media validation before an upload enters the library."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def probe_local_video(path: Path) -> dict[str, Any]:
    binary = shutil.which("ffprobe")
    if not binary:
        return {"probe_state": "unavailable"}
    try:
        completed = subprocess.run(  # noqa: S603 — fixed ffprobe argv, shell disabled
            [
                binary,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,avg_frame_rate:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Recording probe timed out") from exc
    if completed.returncode:
        raise ValueError("Recording could not be decoded as video")
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    if not streams:
        raise ValueError("Recording contains no video stream")
    stream = streams[0]
    rate = stream.get("avg_frame_rate", "0/1").split("/")
    numerator, denominator = float(rate[0]), float(rate[1]) if len(rate) > 1 else 1.0
    duration = float(payload.get("format", {}).get("duration", 0))
    if duration <= 0 or not stream.get("width") or not stream.get("height"):
        raise ValueError("Recording has no usable video frames")
    return {
        "probe_state": "validated",
        "duration_seconds": duration,
        "fps": numerator / denominator if denominator else None,
        "width": stream["width"],
        "height": stream["height"],
        "codec": stream.get("codec_name"),
    }
