"""Stage 2 — Play Segmentation.

Thin orchestrator on top of the play-boundary segmenter adapters in
``pipeline.segment_models`` (Issue #75). The adapter is selected via
the ``SEGMENTER_VARIANT`` environment variable; default stays
``optical_flow`` so the same-session latency budget from Issue #16 is
unchanged.

Variants:
  - ``optical_flow`` (default): the historical mean-flow-magnitude
    heuristic. Same-session safe.
  - ``learned_play``: rules + ML hybrid with a play-duration prior.
    Opt-in candidate from the Phase 2.5 spike.
  - ``sportsbd``: experimental adapter; benchmark only. See
    ``docs/segment-benchmark.md``.
  - ``stub``: deterministic, used by tests.

All boundaries are tagged ``boundary_source="model"`` (preserved for
backward compatibility with the clips API and downstream consumers).
The model identifier itself is logged via ``boundary_model`` so
operators can see which adapter ran. ``boundary_confidence`` is now
model-derived rather than a flat placeholder. Manual overrides are
still handled by coaches via PATCH /api/v1/clips/{id}.

Output: ``clips`` rows for each proposed play segment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2
import structlog

from pipeline import backend, r2
from pipeline.segment_models import (
    OPTICAL_FLOW,
    SegmenterBase,
    get_segmenter,
)

log = structlog.get_logger(__name__)

# Tuning knob — kept here (not in the adapters) because it is a
# *post-processing* filter applied to whatever boundaries the adapter
# returns, not a property of any particular segmentation algorithm.
MIN_PLAY_DURATION = 3.0  # seconds — ignore very short segments


def run(video_id: str, input_uri: str, job_id: str, *, priority: int = 0) -> dict[str, Any]:
    """Run Stage 2: propose clip boundaries and write them to the backend.

    ``priority`` selects the clip result tier (Issue #147): same-session jobs
    produce ``preliminary`` clips (a first pass awaiting the nightly upgrade);
    nightly jobs produce ``final`` clips.
    """
    variant = os.environ.get("SEGMENTER_VARIANT", OPTICAL_FLOW)
    segmenter = get_segmenter(variant)
    log.info(
        "stage_segment_start",
        video_id=video_id,
        variant=variant,
        adapter=type(segmenter).__name__,
        priority=priority,
    )

    r2_key = _uri_to_r2_key(input_uri)
    video_path = r2.download_to_temp(r2_key)
    try:
        return _segment(video_id, video_path, job_id, segmenter, priority)
    finally:
        video_path.unlink(missing_ok=True)


def _uri_to_r2_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


def _video_duration(video_path: Path) -> float:
    """Probe duration via OpenCV — cheap second open after the segmenter."""
    from pipeline.hwaccel import nvdec_video_capture

    cap = nvdec_video_capture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return float(total / fps) if fps else 0.0


def _segment(
    video_id: str,
    video_path: Path,
    job_id: str,
    segmenter: SegmenterBase,
    priority: int = 0,
) -> dict[str, Any]:
    from pipeline.model_router import is_same_session

    boundaries = segmenter.segment(video_path)
    duration = _video_duration(video_path)
    # Same-session clips are a first pass (preliminary); nightly clips are final.
    result_state = "preliminary" if is_same_session(priority) else "final"
    clips = _write_clips(
        video_id, boundaries, duration, job_id, segmenter.source, result_state
    )

    log.info(
        "stage_segment_done",
        video_id=video_id,
        clip_count=len(clips),
        boundary_count=len(boundaries),
        boundary_model=segmenter.source,
    )
    return {
        "clip_count": len(clips),
        "clip_ids": [c["id"] for c in clips],
        # Boundary records for in-process consumers: the orchestrator slices
        # per-frame detections by each clip's [start_time, end_time] window.
        "clips": [
            {
                "id": c.get("id"),
                "start_time": c.get("start_time"),
                "end_time": c.get("end_time"),
                "play_number": c.get("play_number"),
                "result_state": c.get("result_state"),
            }
            for c in clips
        ],
        "boundary_model": segmenter.source,
    }


def _write_clips(
    video_id: str,
    boundaries: list[dict[str, Any]],
    duration: float,
    job_id: str,
    boundary_model: str,
    result_state: str | None = None,
) -> list[dict[str, Any]]:
    """Post one clip per segment to the backend.

    Boundary confidence per segment is the *minimum* of its two
    surrounding boundaries (start/end of the segment) — a segment is
    only as trustworthy as its weakest edge. The leading edge (start
    of the video) and trailing edge (end of the video) are treated as
    fully trusted boundaries with confidence 1.0.
    """
    pivots: list[tuple[float, float]] = [(0.0, 1.0)]
    for b in boundaries:
        pivots.append((float(b["time_s"]), float(b["confidence"])))
    pivots.append((duration, 1.0))
    pivots.sort(key=lambda p: p[0])

    clips: list[dict[str, Any]] = []
    play_idx = 0
    for (start_t, start_c), (end_t, end_c) in zip(pivots, pivots[1:]):
        if end_t - start_t < MIN_PLAY_DURATION:
            continue
        play_idx += 1
        confidence = round(min(start_c, end_c), 3)
        clip = backend.create_clip(
            video_id,
            start_time=start_t,
            end_time=end_t,
            boundary_source="model",
            boundary_confidence=confidence,
            play_number=play_idx,
            job_id=job_id,
            result_state=result_state,
        )
        # Tag the in-memory clip dict so callers/tests can see which
        # adapter produced it without re-querying the backend.
        clip.setdefault("boundary_model", boundary_model)
        clips.append(clip)

    return clips
