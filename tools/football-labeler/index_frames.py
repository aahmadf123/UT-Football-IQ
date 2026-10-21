"""Build an actual source-frame PTS index for a staff dense-track assignment.

Reads the original without modification and hashes it in bounded chunks. The
output is a new file, never an overwrite. ffprobe must be installed on PATH.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    source = next((item for item in catalog["sources"] if item["id"] == args.source), None)
    if source is None:
        parser.error("source ID was not found in the selected catalog")
    media = Path(source["path"]).resolve(strict=True)
    if not all(math.isfinite(value) for value in [args.start, args.end]) or not 0 <= args.start < args.end <= source["duration_seconds"] + .001:
        raise ValueError("Choose a positive source interval inside the original")
    before = media.stat()
    if before.st_size != source["bytes"]:
        raise ValueError("Original byte size differs from the catalog")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required; no frame timestamps will be fabricated")
    hasher = hashlib.sha256()
    with media.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    after = media.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Original changed during hashing; freeze a stable source first")
    if source.get("media_sha256") and source["media_sha256"] != digest:
        raise ValueError("Original SHA-256 differs from the confirmed catalog")
    probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{args.start}%{args.end}", "-show_format", "-show_streams", "-show_frames", "-show_entries", "format=start_time:stream=start_time,width,height:stream_tags=rotate:stream_side_data=rotation:frame=best_effort_timestamp_time", "-of", "json", str(media)], check=True, capture_output=True, text=True, timeout=600)
    measured = json.loads(probe.stdout)
    stream_info = measured["streams"][0]
    origin = float(stream_info.get("start_time", "nan"))
    container_origin = float(measured.get("format", {}).get("start_time", "nan"))
    if not all(math.isfinite(value) and abs(value) <= .000001 for value in [origin, container_origin]):
        raise ValueError("Dense frame indexing currently requires a proven zero-origin source clock; offset media need verified browser/PTS normalization")
    width, height = stream_info["width"], stream_info["height"]
    if (width, height) != (source["width"], source["height"]):
        raise ValueError("Actual source dimensions differ from the catalog")
    rotations = [stream_info.get("tags", {}).get("rotate", 0), *[item.get("rotation", 0) for item in stream_info.get("side_data_list", [])]]
    if any(not math.isfinite(float(value)) or abs(float(value)) > .000001 for value in rotations):
        raise ValueError("Rotated media require a verified displayed-pixel coordinate conversion before dense indexing")
    final = media.stat()
    if (before.st_size, before.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise ValueError("Original changed during frame inspection")
    points = [float(frame["best_effort_timestamp_time"]) for frame in measured["frames"] if "best_effort_timestamp_time" in frame]
    points = [value for value in points if math.isfinite(value) and args.start <= value <= args.end]
    if len(points) < 2 or any(right <= left for left, right in zip(points, points[1:])):
        raise ValueError("Frame PTS must contain at least two strictly increasing actual observations")
    result = {"schema_version": 1, "evidence_kind": "source_frame_index", "source_id": source["id"], "media_sha256": digest, "source_clock_origin_seconds": origin, "width": width, "height": height, "start_seconds": args.start, "end_seconds": args.end, "frame_pts_seconds": points, "note": "Source frame times only; this is not an annotation or proof of dense coverage."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"source_id": source["id"], "actual_frames": len(points), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
