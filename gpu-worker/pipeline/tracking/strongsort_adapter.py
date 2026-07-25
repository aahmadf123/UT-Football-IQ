"""StrongSORT tracker adapter (Issue #129).

StrongSORT is the best-IDF1 option for *offline* nightly post-processing. It
builds on the same constant-velocity motion model as BoT-SORT
(``pipeline.tracking.botsort_adapter``) and adds:

  * a **matching cascade** — tracks seen more recently get first pick of the
    detections, which suppresses ID swaps in crowded scenes; and
  * heavier reliance on **appearance** (EMA-smoothed embeddings) than motion,
    because StrongSORT is meant to spend nightly compute on a strong ReID
    signal.

Like BoT-SORT it only ever receives ``detections_by_frame`` and degrades to a
motion-only tracker when no appearance embeddings are present, so it stays
inside the ``TrackerBase`` contract and is testable without weights or a GPU.

Routing: StrongSORT is **nightly-only** (``NIGHTLY_ONLY_VARIANTS``). It is not
a default in any bucket; it is reachable for the nightly ``track`` bucket via
``MODEL_ROUTING_CONFIG``. It never runs same-session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import structlog

from pipeline.tracker_models import TrackResult, _greedy_assign
from pipeline.tracking.botsort_adapter import BoTSORTTracker, _MotionTrack

log = structlog.get_logger(__name__)


class StrongSORTTracker(BoTSORTTracker):
    """Matching-cascade, appearance-leaning offline tracker.

    Args mirror :class:`BoTSORTTracker`, but ``appearance_weight`` defaults to
    ``0.6`` (appearance-dominant) and association runs as a cascade over track
    age rather than a single global greedy pass.
    """

    mask_aware = False

    def __init__(
        self,
        iou_threshold: float = 0.25,
        max_lost_frames: int = 50,
        appearance_weight: float = 0.6,
        camera_motion: dict[int, np.ndarray] | None = None,
        appearance_fn: Callable[[dict[str, Any]], np.ndarray] | None = None,
    ) -> None:
        super().__init__(
            iou_threshold=iou_threshold,
            max_lost_frames=max_lost_frames,
            appearance_weight=appearance_weight,
            camera_motion=camera_motion,
            appearance_fn=appearance_fn,
        )

    def _cascade_assign(
        self,
        active: list[_MotionTrack],
        frame_dets: list[dict[str, Any]],
        frame_idx: int,
    ) -> tuple[set[int], set[int]]:
        """Greedily assign by ascending ``lost_count`` (matching cascade).

        Returns ``(matched_track_indices, matched_det_indices)`` against the
        original ``active`` / ``frame_dets`` ordering.
        """
        matched_tracks: set[int] = set()
        matched_dets: set[int] = set()
        ages = sorted({t.lost_count for t in active})
        for age in ages:
            bucket = [i for i, t in enumerate(active) if t.lost_count == age]
            free_dets = [j for j in range(len(frame_dets)) if j not in matched_dets]
            if not bucket or not free_dets:
                continue
            cost = [
                [self._score(active[ti], frame_dets[dj], frame_idx) for dj in free_dets]
                for ti in bucket
            ]
            for local_ti, local_dj in _greedy_assign(cost, self.iou_threshold):
                ti = bucket[local_ti]
                dj = free_dets[local_dj]
                warp = self._chained_warp(active[ti].last_frame, frame_idx)
                active[ti].update(frame_dets[dj], frame_idx, warp)
                matched_tracks.add(ti)
                matched_dets.add(dj)
        return matched_tracks, matched_dets

    def track(
        self,
        detections_by_frame: dict[str, list[dict[str, Any]]],
    ) -> list[TrackResult]:
        active: list[_MotionTrack] = []
        closed: list[_MotionTrack] = []
        next_id = 1

        for frame_idx in sorted(int(f) for f in detections_by_frame):
            frame_dets = [
                d
                for d in detections_by_frame.get(str(frame_idx), [])
                if d.get("class", "player") == "player"
            ]

            if active and frame_dets:
                matched_tracks, matched_dets = self._cascade_assign(
                    active, frame_dets, frame_idx
                )
                for di, det in enumerate(frame_dets):
                    if di not in matched_dets:
                        active.append(_MotionTrack(next_id, det, frame_idx))
                        next_id += 1
                for ti, t in enumerate(active):
                    if ti not in matched_tracks and t.last_frame != frame_idx:
                        t.lost_count += 1
            else:
                for t in active:
                    t.lost_count += 1
                for det in frame_dets:
                    active.append(_MotionTrack(next_id, det, frame_idx))
                    next_id += 1

            still_active: list[_MotionTrack] = []
            for t in active:
                if t.lost_count > self.max_lost_frames:
                    closed.append(t)
                else:
                    still_active.append(t)
            active = still_active

        closed.extend(active)
        return [t.finalise() for t in closed]
