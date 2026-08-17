"""Sample frames from footage and upload them to the consolidated project.

The training set must cover every capture regime the product accepts (phone,
drone, fixed sideline — any height/angle/resolution), so every upload carries
``regime:``/``session:``/``clip:`` tags; balancing the dataset later is a
tag query, not a re-shoot.

Sampling: uniform stride with perceptual-hash dedupe (average hash) so
near-identical consecutive frames don't flood the dataset.

Usage:
    python -m roboflow_ops.frames --input "../Drone Footage" \
        --per-video 12 --regime drone_follow --session practice
    python -m roboflow_ops.frames --input clip.mp4 --no-upload   # extract only
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

from roboflow_ops.client import get_project, load_config, upload_image

log = structlog.get_logger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
# Hamming distance at or below which two 64-bit average-hashes are "the same
# moment" — tuned loose enough to drop consecutive near-duplicates only.
DEDUPE_MAX_HAMMING = 4


def average_hash(gray_8x8: Any) -> int:
    """64-bit average hash of an 8x8 grayscale array (numpy)."""
    mean = float(gray_8x8.mean())
    bits = 0
    flat = gray_8x8.flatten()
    for i in range(64):
        if float(flat[i]) > mean:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_duplicate(candidate: int, kept: list[int], max_distance: int = DEDUPE_MAX_HAMMING) -> bool:
    return any(hamming(candidate, h) <= max_distance for h in kept)


def sample_stride(frame_count: int, per_video: int) -> int:
    """Stride that yields roughly ``per_video`` samples over ``frame_count``."""
    if per_video <= 0:
        return max(frame_count, 1)
    return max(frame_count // per_video, 1)


def iter_videos(input_path: Path) -> Iterator[Path]:
    if input_path.is_file():
        yield input_path
        return
    for p in sorted(input_path.rglob("*")):
        if p.suffix.lower() in VIDEO_SUFFIXES:
            yield p


def extract_frames(video_path: Path, out_dir: Path, per_video: int) -> list[Path]:
    """Decode + sample + dedupe frames from one video; returns written jpgs."""
    import cv2  # lazy: heavy dependency, not in CI stub mode

    from pipeline.video_ingest import LocalFileVideoSource

    source = LocalFileVideoSource(video_path)
    stride = sample_stride(source.total_frames, per_video)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    kept_hashes: list[int] = []
    for frame_number, frame in source.iter_frames(stride=stride):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        tiny = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
        h = average_hash(tiny)
        if is_duplicate(h, kept_hashes):
            continue
        kept_hashes.append(h)
        out = out_dir / f"{video_path.stem}_f{frame_number:06d}.jpg"
        cv2.imwrite(str(out), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        written.append(out)
        if per_video > 0 and len(written) >= per_video:
            break
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Video file or directory")
    parser.add_argument("--per-video", type=int, default=12, help="Frames per video (approx)")
    parser.add_argument("--regime", default=None, help="Capture regime tag (e.g. drone_follow)")
    parser.add_argument("--session", default=None, help="Session kind tag (practice/game)")
    parser.add_argument("--batch", default="frames", help="Roboflow batch name")
    parser.add_argument("--no-upload", action="store_true", help="Extract locally only")
    args = parser.parse_args()

    cfg = load_config()
    frames_root = cfg.data_dir / "frames"
    project = None if args.no_upload else get_project(cfg)

    total_extracted = 0
    total_uploaded = 0
    for video in iter_videos(Path(args.input)):
        frames = extract_frames(video, frames_root / video.stem, args.per_video)
        total_extracted += len(frames)
        log.info("frames_extracted", video=video.name, count=len(frames))
        if project is None:
            continue
        tags = [f"clip:{video.stem}"]
        if args.regime:
            tags.append(f"regime:{args.regime}")
        if args.session:
            tags.append(f"session:{args.session}")
        for frame_path in frames:
            if upload_image(project, frame_path, batch_name=args.batch, tags=tags):
                total_uploaded += 1

    print(
        f"Extracted {total_extracted} frames"
        + ("" if args.no_upload else f", uploaded {total_uploaded}")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
