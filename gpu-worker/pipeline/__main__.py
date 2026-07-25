"""Turnkey local pipeline CLI — any video file in, boxes/clips/overlays out.

Usage::

    python -m pipeline run --input "Drone Footage/Dji 0257.mp4" --out ./out
    python -m pipeline run --input footage_dir/ --mode nightly --stride 5
    python -m pipeline run --input clip.mp4 --stages ingest,segment,detect

No backend, queue, or cloud credentials are required: ``--no-backend`` is
the default when ``BACKEND_API_URL`` is unset. Any filename, any camera,
any angle — capture constraints are quality hints, not gates.

Environment staging matters: ``pipeline.backend`` and several stages read
their env at import time, so this module parses arguments and sets
``BACKEND_API_URL`` / ``LOCAL_STORAGE_ROOT`` / ``DETECT_FRAME_STEP``
**before** importing the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".webm"}

SAME_SESSION_CLI_PRIORITY = 10
NIGHTLY_CLI_PRIORITY = 0


def _collect_inputs(raw: str) -> list[Path]:
    path = Path(raw).expanduser()
    if path.is_file():
        return [path]
    if path.is_dir():
        files = sorted(
            p for p in path.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()
        )
        if not files:
            raise SystemExit(f"No video files found in directory: {path}")
        return files
    raise SystemExit(f"Input not found: {path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="Run the Football-IQ processing pipeline on local video files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Process a video file or a directory of them")
    run_p.add_argument("--input", required=True, help="Video file or directory")
    run_p.add_argument(
        "--out",
        default="./out",
        help="Output root; per-video results land in {out}/{video-stem}/ (default ./out)",
    )
    run_p.add_argument(
        "--mode",
        choices=("same_session", "nightly"),
        default="same_session",
        help="same_session = fast preview models; nightly = full quality (default same_session)",
    )
    run_p.add_argument(
        "--stages",
        default=None,
        help="Comma-separated stage subset (default: the mode's full list)",
    )
    run_p.add_argument(
        "--stride",
        type=int,
        default=None,
        help="Detection frame step override (DETECT_FRAME_STEP)",
    )
    run_p.add_argument(
        "--video-id",
        default=None,
        help="Existing backend video id (requires BACKEND_API_URL; single-file input only)",
    )
    run_p.add_argument(
        "--no-backend",
        action="store_true",
        help="Force offline mode even if BACKEND_API_URL is set",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command != "run":  # pragma: no cover — argparse enforces this
        raise SystemExit(2)

    inputs = _collect_inputs(args.input)
    if args.video_id and len(inputs) > 1:
        raise SystemExit("--video-id only makes sense with a single input file")

    # ── Stage the environment BEFORE any pipeline import ──────────────────
    if args.no_backend or not os.environ.get("BACKEND_API_URL"):
        os.environ["BACKEND_API_URL"] = ""
    if args.stride is not None:
        os.environ["DETECT_FRAME_STEP"] = str(args.stride)

    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    # Rendered overlays flow through the storage facade; point it at the
    # output tree (unless the operator already routed it elsewhere).
    os.environ.setdefault("STORAGE_BACKEND", "local")
    os.environ.setdefault("LOCAL_STORAGE_ROOT", str(out_root / "storage"))

    from pipeline.orchestrator import DirArtifactSink, run_pipeline

    priority = (
        SAME_SESSION_CLI_PRIORITY if args.mode == "same_session" else NIGHTLY_CLI_PRIORITY
    )
    stages = [s.strip() for s in args.stages.split(",") if s.strip()] if args.stages else None

    def progress(stage: str, clip_id: str | None, status: str, extra: dict) -> None:
        scope = f"[{clip_id[:8]}]" if clip_id else "[video]"
        detail = " ".join(f"{k}={v}" for k, v in extra.items())
        print(f"  {scope} {stage:<10} {status:<9} {detail}".rstrip(), flush=True)

    exit_code = 0
    for source in inputs:
        video_id = args.video_id or str(uuid.uuid4())
        out_dir = out_root / source.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n▶ {source.name}  (video_id={video_id}, mode={args.mode})", flush=True)

        sink = DirArtifactSink(out_dir / "artifacts")
        try:
            summary = run_pipeline(
                video_id,
                str(source.resolve()),
                priority=priority,
                job_id=f"cli-{video_id[:8]}",
                stages=stages,
                progress_cb=progress,
                artifact_sink=sink,
            )
        except Exception as exc:  # keep processing remaining files
            print(f"✗ {source.name} failed: {exc}", file=sys.stderr, flush=True)
            exit_code = 1
            continue

        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        overlays = [c.get("overlay_uri") for c in summary["clips"] if c.get("overlay_uri")]
        print(
            f"✓ {source.name}: {summary['clip_count']} clips, "
            f"{sum(c['tracklet_count'] for c in summary['clips'])} tracklets, "
            f"{len(overlays)} overlays  →  {out_dir}",
            flush=True,
        )
        if summary["failed_clip_stages"]:
            print(
                f"  ⚠ {len(summary['failed_clip_stages'])} clip-stage failures "
                f"(see summary.json)",
                flush=True,
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
