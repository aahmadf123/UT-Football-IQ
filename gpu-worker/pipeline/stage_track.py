"""Stage 5 — Player Tracking.

Delegates association to a tracker adapter from
:mod:`pipeline.tracker_models`:

  - ``IoUTracker``         — production same-session path (bbox IoU)
  - ``SAM3MaskTracker``    — experimental nightly path, mask-aware
  - ``StubTracker``        — deterministic tests

Tracklet schema written to the backend remains the same; the only
extension is that ``track_points`` may carry an optional ``mask`` field
when the upstream detector produced one.  Backends that ignore the field
keep working unchanged.
"""

from __future__ import annotations

from typing import Any

import structlog

from pipeline import backend
from pipeline.tracker_models import TrackerBase, get_tracker

log = structlog.get_logger(__name__)

MAX_LOST_FRAMES = 30  # frames a track can be "lost" before it is closed
IOU_THRESHOLD = 0.3


def run(
    clip_id: str,
    detections: dict[str, list[dict[str, Any]]],
    fps: float,
    job_id: str,
    *,
    tracker: TrackerBase | None = None,
    variant: str | None = None,
) -> dict[str, Any]:
    """Run tracking and write tracklets to the backend.

    Args:
        clip_id, detections, fps, job_id: pipeline-standard arguments.
        tracker:  Pre-built tracker adapter; overrides ``variant``.
        variant:  Routing variant id (e.g. ``"iou-tracker"``,
                  ``"sam3-mask-tracker"``).  Defaults to ``"iou-tracker"``.
    """
    log.info("stage_track_start", clip_id=clip_id, variant=variant)

    if tracker is None:
        tracker = get_tracker(
            variant or "iou-tracker",
            iou_threshold=IOU_THRESHOLD,
            max_lost_frames=MAX_LOST_FRAMES,
        )

    results = tracker.track(detections)

    tracklet_ids: list[str] = []
    tracklets: list[dict[str, Any]] = []
    masked_tracklet_count = 0
    for track in results:
        if len(track.points) < 2:
            continue
        try:
            resp = backend.create_tracklet(
                clip_id,
                start_frame=track.start_frame,
                end_frame=track.last_frame,
                track_points=track.points,
                team_label="unknown",
                job_id=job_id,
            )
            tracklet_ids.append(resp["id"])
            # Full tracklet record for in-process consumers (reid / pose /
            # events / labels / metrics take `tracklets` dicts in exactly
            # the backend row shape — carrying them here saves the
            # orchestrator a read-back round trip).
            tracklets.append(
                {
                    "id": resp["id"],
                    "clip_id": clip_id,
                    "start_frame": track.start_frame,
                    "end_frame": track.last_frame,
                    "track_points": track.points,
                    "team_label": "unknown",
                }
            )
            if any("mask" in p for p in track.points):
                masked_tracklet_count += 1
        except Exception as exc:
            log.warning("tracklet_write_failed", track_id=track.track_id,
                        error=str(exc))

    log.info(
        "stage_track_done",
        clip_id=clip_id,
        tracklet_count=len(tracklet_ids),
        masked_tracklet_count=masked_tracklet_count,
        mask_aware=tracker.mask_aware,
    )
    return {
        "tracklet_count": len(tracklet_ids),
        "tracklet_ids": tracklet_ids,
        "tracklets": tracklets,
        "mask_aware": tracker.mask_aware,
    }
