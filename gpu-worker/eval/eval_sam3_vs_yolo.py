"""SAM 3.1 vs YOLO + IoU baseline evaluation (Issue #74).

Computes detection-quality and tracking-stability metrics for both
variants on the same input clips so we can decide whether SAM 3.1 is
ready to graduate from nightly-experimental to a same-session option.

Metrics:
  Detection
    - mean detections / frame
    - frame coverage (% of sampled frames with ≥ 1 detection)
    - mean confidence
    - mask coverage (% of detections that carry a mask)

  Tracking
    - track count
    - mean track length (frames)
    - max track length
    - fragmentation index — track_count / unique_player_estimate
    - ID-switch proxy — fraction of tracks shorter than 5 frames

Usage:

    # Real footage — requires HF_TOKEN for SAM 3.1 weights.
    python -m eval.eval_sam3_vs_yolo \\
        --clip-dir data/eval-clips/sample/ \\
        --frame-step 3 \\
        --out reports/phase2-issue74-sam3-eval.json

    # CI-safe stub run (no weights, no GPU) — used by the test suite.
    python -m eval.eval_sam3_vs_yolo --synthetic --out /tmp/eval.json

The script is intentionally side-effect free for the synthetic path so
it can be invoked from a unit test as well as a CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Allow running both as ``python -m eval.eval_sam3_vs_yolo`` and via the
# project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.detector_models import (  # noqa: E402
    DetectorBase,
    StubDetector,
    detection_has_mask,
    get_detector,
)
from pipeline.tracker_models import (  # noqa: E402
    IoUTracker,
    SAM3MaskTracker,
    TrackerBase,
)

# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class DetectionMetrics:
    variant: str
    frame_count: int
    detection_count: int
    detected_frames: int
    mean_detections_per_frame: float
    frame_coverage: float
    mean_confidence: float
    mask_coverage: float

    @property
    def summary(self) -> str:
        return (
            f"{self.variant}: {self.detection_count} dets / "
            f"{self.frame_count} frames "
            f"({self.frame_coverage:.1%} coverage, "
            f"mean conf {self.mean_confidence:.2f}, "
            f"masks {self.mask_coverage:.1%})"
        )


@dataclass
class TrackingMetrics:
    variant: str
    tracker: str
    track_count: int
    mean_track_length: float
    max_track_length: int
    fragmentation_index: float
    short_track_fraction: float

    @property
    def summary(self) -> str:
        return (
            f"{self.variant} → {self.tracker}: {self.track_count} tracks, "
            f"mean len {self.mean_track_length:.1f}, "
            f"max {self.max_track_length}, "
            f"frag {self.fragmentation_index:.2f}, "
            f"short<5 {self.short_track_fraction:.1%}"
        )


@dataclass
class EvalResult:
    detection: list[DetectionMetrics] = field(default_factory=list)
    tracking: list[TrackingMetrics] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Metric computation ────────────────────────────────────────────────────────


def _detection_metrics(
    variant: str,
    detections_by_frame: dict[str, list[dict[str, Any]]],
    sampled_frame_count: int,
) -> DetectionMetrics:
    total = 0
    confs: list[float] = []
    masks = 0
    for dets in detections_by_frame.values():
        for d in dets:
            total += 1
            confs.append(float(d.get("confidence", 0.0)))
            if detection_has_mask(d):
                masks += 1
    detected_frames = sum(1 for dets in detections_by_frame.values() if dets)
    denom = max(sampled_frame_count, 1)
    return DetectionMetrics(
        variant=variant,
        frame_count=sampled_frame_count,
        detection_count=total,
        detected_frames=detected_frames,
        mean_detections_per_frame=total / denom,
        frame_coverage=detected_frames / denom,
        mean_confidence=(sum(confs) / len(confs)) if confs else 0.0,
        mask_coverage=(masks / total) if total else 0.0,
    )


def _tracking_metrics(
    variant: str,
    tracker_name: str,
    tracker: TrackerBase,
    detections_by_frame: dict[str, list[dict[str, Any]]],
    unique_player_estimate: int,
) -> TrackingMetrics:
    tracks = tracker.track(detections_by_frame)
    lengths = [len(t.points) for t in tracks]
    track_count = len(tracks)
    short = sum(1 for n in lengths if n < 5)
    return TrackingMetrics(
        variant=variant,
        tracker=tracker_name,
        track_count=track_count,
        mean_track_length=(sum(lengths) / track_count) if track_count else 0.0,
        max_track_length=max(lengths) if lengths else 0,
        fragmentation_index=(
            track_count / max(unique_player_estimate, 1)
            if track_count else 0.0
        ),
        short_track_fraction=(short / track_count) if track_count else 0.0,
    )


# ── Drivers ───────────────────────────────────────────────────────────────────


def _detect_clip(
    detector: DetectorBase,
    frames: list[Any],
    frame_step: int,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for i, frame in enumerate(frames):
        if i % frame_step != 0:
            continue
        dets = detector.detect(frame)
        if dets:
            out[str(i)] = dets
    return out


def _iter_clip_frames(clip_path: Path) -> list[Any]:
    """Read every frame of a clip via OpenCV.  Imported lazily so CI
    without opencv-python-headless can still run the synthetic path."""
    import cv2  # type: ignore[import-untyped]

    cap = cv2.VideoCapture(str(clip_path))
    frames = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frames.append(frame)
    cap.release()
    return frames


def _synthetic_frames(n: int = 120, w: int = 1280, h: int = 720) -> list[Any]:
    """Build a list of cheap zero-frames for the no-GPU eval path.

    Using numpy zeros (rather than calling cv2) keeps this path
    runnable in CI without opencv-python-headless.
    """
    import numpy as np
    return [np.zeros((h, w, 3), dtype=np.uint8) for _ in range(n)]


def run_eval(
    clip_paths: list[Path] | None = None,
    *,
    frame_step: int = 3,
    synthetic: bool = False,
    unique_player_estimate: int = 22,
) -> EvalResult:
    """Run the SAM 3.1 vs YOLO comparison and return aggregated metrics.

    Args:
        clip_paths: Real clips to run on.  Ignored when ``synthetic=True``.
        frame_step: Match the in-production detection cadence.
        synthetic: When True, use ``StubDetector`` outputs for both
                   variants.  Lets CI exercise the eval pipeline without
                   weights / GPU / network.
        unique_player_estimate: Used to normalise the fragmentation
                                index. Default 22 (full football lineup).
    """
    result = EvalResult()

    # Construct detectors first.  In synthetic mode we wire up stubs
    # tagged with the variant name; otherwise we use the real factory.
    if synthetic:
        yolo_detector: DetectorBase = StubDetector(n_players=11, produces_masks=False)
        sam_detector: DetectorBase = StubDetector(n_players=11, produces_masks=True)
        result.notes.append(
            "synthetic mode: stubs used in place of YOLO and SAM 3.1; "
            "metrics reflect schema/plumbing only, not model quality."
        )
        frames_per_clip = [_synthetic_frames()]
    else:
        if not clip_paths:
            raise ValueError("clip_paths required when synthetic=False")
        yolo_detector = get_detector("yolov8n")
        sam_detector = get_detector("sam3.1")
        frames_per_clip = [_iter_clip_frames(p) for p in clip_paths]

    yolo_dets_all: dict[str, list[dict[str, Any]]] = {}
    sam_dets_all: dict[str, list[dict[str, Any]]] = {}
    total_sampled = 0
    clip_offset = 0
    for frames in frames_per_clip:
        if not frames:
            continue
        sampled = (len(frames) + frame_step - 1) // frame_step
        total_sampled += sampled
        yolo_clip = _detect_clip(yolo_detector, frames, frame_step)
        sam_clip = _detect_clip(sam_detector, frames, frame_step)
        # Rebase frame indices so multi-clip eval doesn't collide.
        for k, v in yolo_clip.items():
            yolo_dets_all[str(int(k) + clip_offset)] = v
        for k, v in sam_clip.items():
            sam_dets_all[str(int(k) + clip_offset)] = v
        clip_offset += len(frames)

    result.detection.append(
        _detection_metrics("yolov8n", yolo_dets_all, total_sampled)
    )
    result.detection.append(
        _detection_metrics("sam3.1", sam_dets_all, total_sampled)
    )

    # Tracking — pair each detector with its production tracker plus the
    # opposite tracker so we can see the contribution of the mask-aware
    # association independently of detection quality.
    pairs: list[tuple[str, str, TrackerBase, dict[str, list[dict[str, Any]]]]] = [
        ("yolov8n", "iou-tracker", IoUTracker(), yolo_dets_all),
        ("sam3.1", "iou-tracker", IoUTracker(), sam_dets_all),
        ("sam3.1", "sam3-mask-tracker", SAM3MaskTracker(), sam_dets_all),
    ]
    for variant, tname, tracker, dets in pairs:
        result.tracking.append(
            _tracking_metrics(variant, tname, tracker, dets,
                              unique_player_estimate)
        )

    return result


# ── CLI entrypoint ────────────────────────────────────────────────────────────


def _to_jsonable(result: EvalResult) -> dict[str, Any]:
    return {
        "detection": [asdict(m) for m in result.detection],
        "tracking": [asdict(m) for m in result.tracking],
        "notes": list(result.notes),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SAM 3.1 vs YOLO + IoU evaluation (Issue #74)",
    )
    parser.add_argument(
        "--clip-dir",
        type=Path,
        help="Directory of .mp4 clips to evaluate.",
    )
    parser.add_argument("--frame-step", type=int, default=3)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run without GPU / weights using StubDetector.",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.synthetic:
        result = run_eval(synthetic=True, frame_step=args.frame_step)
    else:
        if not args.clip_dir:
            parser.error("--clip-dir required unless --synthetic is set")
        clips = sorted(args.clip_dir.glob("*.mp4"))
        if not clips:
            parser.error(f"no .mp4 clips found under {args.clip_dir}")
        result = run_eval(clip_paths=clips, frame_step=args.frame_step)

    payload = _to_jsonable(result)
    text = json.dumps(payload, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)

    print(text)
    print()
    for m in result.detection:
        print(m.summary)
    for m in result.tracking:
        print(m.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
