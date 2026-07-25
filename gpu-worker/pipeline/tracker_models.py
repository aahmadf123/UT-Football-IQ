"""Tracker adapters for the Player Tracking stage (Issue #74).

Mirrors the ``pose_estimator`` / ``detector_models`` adapter pattern.

Track-point schema returned by ``track``:

    {
        "frame_number": int,
        "bbox": [x1, y1, x2, y2],         # required — backwards compatible
        "mask": {                           # optional — only set when the
            "format": "polygon",            # detector produced one AND the
            "points": [[x, y], ...]         # tracker chose to propagate it.
        },
    }

Trackers must always emit ``bbox``.  ``mask`` is opt-in: downstream stages
(stage_track, stage_pose, …) MUST treat it as optional so the long
running YOLO + IoU path keeps working untouched.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import structlog

from pipeline.detector_models import detection_has_mask, polygon_area

log = structlog.get_logger(__name__)


# ── Public schema constants ───────────────────────────────────────────────────

IOU_TRACKER = "iou-tracker"
SAM3_MASK_TRACKER = "sam3-mask-tracker"
BOTSORT = "botsort"
STRONGSORT = "strongsort"
STUB_TRACKER = "stub"


# ── Base class ────────────────────────────────────────────────────────────────


class TrackResult:
    """A finalised track for a single object.

    Attributes:
        track_id:    Per-clip integer identifier.
        start_frame: First frame the track was observed.
        last_frame:  Last frame the track was observed.
        points:      List of track-point dicts (see module docstring).
    """

    __slots__ = ("track_id", "start_frame", "last_frame", "points")

    def __init__(
        self,
        track_id: int,
        start_frame: int,
        last_frame: int,
        points: list[dict[str, Any]],
    ) -> None:
        self.track_id = track_id
        self.start_frame = start_frame
        self.last_frame = last_frame
        self.points = points


class TrackerBase(ABC):
    """Abstract base for per-clip multi-object trackers."""

    #: Whether this tracker uses mask information when present.
    mask_aware: bool = False

    @abstractmethod
    def track(
        self,
        detections_by_frame: dict[str, list[dict[str, Any]]],
    ) -> list[TrackResult]:
        """Associate detections across frames into tracks.

        Args:
            detections_by_frame: ``{frame_idx_str: [detection_dict, ...]}``
                where each detection follows the schema documented in
                ``detector_models``.  Only ``player``-class detections are
                tracked; other classes are ignored.

        Returns:
            One :class:`TrackResult` per closed track (length >= 1).
        """


# ── Geometry helpers ──────────────────────────────────────────────────────────


def bbox_iou(a: list[float], b: list[float]) -> float:
    """Standard axis-aligned-bbox IoU.  Returns 0 for non-overlap."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / (area_a + area_b - inter)


def mask_overlap_score(
    mask_a: dict[str, Any] | None,
    mask_b: dict[str, Any] | None,
) -> float:
    """Cheap polygon overlap proxy: ratio of min(area)/max(area).

    A real polygon-polygon IoU needs shapely; we approximate stability by
    comparing areas — players whose masks change size drastically between
    frames score lower.  Returns 0 if either mask is missing or
    degenerate.
    """
    if mask_a is None or mask_b is None:
        return 0.0
    pts_a = mask_a.get("points") or []
    pts_b = mask_b.get("points") or []
    area_a = polygon_area(pts_a)
    area_b = polygon_area(pts_b)
    if area_a <= 0.0 or area_b <= 0.0:
        return 0.0
    return min(area_a, area_b) / max(area_a, area_b)


# ── Greedy assignment used by both IoU and mask-aware trackers ────────────────


def _greedy_assign(
    cost: list[list[float]],
    threshold: float,
) -> list[tuple[int, int]]:
    """Pick the best row→col pair greedily until cost drops below threshold.

    O(n²) — fine for ≤50 players per frame.  Identical to the original
    ``stage_track._hungarian`` helper, lifted here so both trackers can
    share the same association policy.
    """
    n_rows = len(cost)
    n_cols = len(cost[0]) if cost else 0
    used_cols: set[int] = set()
    matches: list[tuple[int, int]] = []
    for r in range(n_rows):
        best_col = -1
        best_val = threshold
        for c in range(n_cols):
            if c not in used_cols and cost[r][c] > best_val:
                best_val = cost[r][c]
                best_col = c
        if best_col >= 0:
            matches.append((r, best_col))
            used_cols.add(best_col)
    return matches


# ── Internal track state ──────────────────────────────────────────────────────


class _Track:
    def __init__(self, track_id: int, det: dict[str, Any], frame: int) -> None:
        self.track_id = track_id
        self.bbox: list[float] = list(det["bbox"])
        self.mask: dict[str, Any] | None = det.get("mask")
        self.start_frame = frame
        self.last_frame = frame
        self.lost_count = 0
        self.points: list[dict[str, Any]] = [self._point(det, frame)]

    def update(self, det: dict[str, Any], frame: int) -> None:
        self.bbox = list(det["bbox"])
        self.mask = det.get("mask", self.mask)
        self.last_frame = frame
        self.lost_count = 0
        self.points.append(self._point(det, frame))

    @staticmethod
    def _point(det: dict[str, Any], frame: int) -> dict[str, Any]:
        point: dict[str, Any] = {
            "frame_number": frame,
            "bbox": list(det["bbox"]),
        }
        if detection_has_mask(det):
            point["mask"] = {
                "format": det["mask"]["format"],
                "points": [list(p) for p in det["mask"]["points"]],
            }
        return point

    def finalise(self) -> TrackResult:
        return TrackResult(
            track_id=self.track_id,
            start_frame=self.start_frame,
            last_frame=self.last_frame,
            points=self.points,
        )


# ── IoU tracker (existing same-session path, factored out) ────────────────────


class IoUTracker(TrackerBase):
    """ByteTrack-style IoU tracker — the production same-session path.

    Reproduces the behaviour of the original ``stage_track._Track`` /
    ``_hungarian`` pair so existing pipelines keep their exact numerical
    output.  Does not look at masks even if the detector emits them.
    """

    mask_aware = False

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_lost_frames: int = 30,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_lost_frames = max_lost_frames

    def track(
        self,
        detections_by_frame: dict[str, list[dict[str, Any]]],
    ) -> list[TrackResult]:
        return _run_association(
            detections_by_frame,
            score_fn=lambda track, det: bbox_iou(track.bbox, det["bbox"]),
            threshold=self.iou_threshold,
            max_lost_frames=self.max_lost_frames,
        )


# ── SAM 3 mask-aware tracker (experimental nightly path) ──────────────────────


class SAM3MaskTracker(TrackerBase):
    """Mask-aware tracker for SAM 3.1 detections.

    Scoring blends bbox IoU with a mask-area-stability term so players
    whose masks shrink/grow drastically between frames (typical of ID
    swaps) score lower.  When a detection has no mask the tracker falls
    back to pure bbox IoU — so it remains safe to use on a mixed batch
    where only some frames were segmented.
    """

    mask_aware = True

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_lost_frames: int = 30,
        mask_weight: float = 0.5,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_lost_frames = max_lost_frames
        # ``mask_weight`` ∈ [0, 1]: how much the mask-overlap term
        # contributes to the combined score relative to bbox IoU.
        self.mask_weight = max(0.0, min(1.0, mask_weight))

    def _score(self, track: _Track, det: dict[str, Any]) -> float:
        iou = bbox_iou(track.bbox, det["bbox"])
        if not detection_has_mask(det) or track.mask is None:
            return iou
        mscore = mask_overlap_score(track.mask, det.get("mask"))
        return (1.0 - self.mask_weight) * iou + self.mask_weight * mscore

    def track(
        self,
        detections_by_frame: dict[str, list[dict[str, Any]]],
    ) -> list[TrackResult]:
        return _run_association(
            detections_by_frame,
            score_fn=self._score,
            threshold=self.iou_threshold,
            max_lost_frames=self.max_lost_frames,
        )


# ── Stub tracker ──────────────────────────────────────────────────────────────


class StubTracker(TrackerBase):
    """Deterministic stub — assigns each detection to its own track.

    Useful for tests that just need a finite, predictable set of tracks
    without exercising the association logic.
    """

    mask_aware = False

    def track(
        self,
        detections_by_frame: dict[str, list[dict[str, Any]]],
    ) -> list[TrackResult]:
        tracks: dict[int, _Track] = {}
        next_id = 1
        for frame_str in sorted(detections_by_frame, key=int):
            frame = int(frame_str)
            dets = [d for d in detections_by_frame[frame_str]
                    if d.get("class", "player") == "player"]
            for i, det in enumerate(dets):
                tid = next_id + i
                if tid in tracks:
                    tracks[tid].update(det, frame)
                else:
                    tracks[tid] = _Track(tid, det, frame)
            next_id += len(dets)
        return [t.finalise() for t in tracks.values()]


# ── Shared association loop ───────────────────────────────────────────────────


def _run_association(
    detections_by_frame: dict[str, list[dict[str, Any]]],
    *,
    score_fn,
    threshold: float,
    max_lost_frames: int,
) -> list[TrackResult]:
    """Frame-by-frame greedy association driver shared by every tracker."""
    active: list[_Track] = []
    closed: list[_Track] = []
    next_id = 1

    for frame_idx in sorted(int(f) for f in detections_by_frame):
        frame_dets = [
            d for d in detections_by_frame.get(str(frame_idx), [])
            if d.get("class", "player") == "player"
        ]

        if active and frame_dets:
            cost = [[score_fn(t, d) for d in frame_dets] for t in active]
            matches = _greedy_assign(cost, threshold)
            matched_tracks = {m[0] for m in matches}
            matched_dets = {m[1] for m in matches}

            for ti, di in matches:
                active[ti].update(frame_dets[di], frame_idx)

            for di, det in enumerate(frame_dets):
                if di not in matched_dets:
                    active.append(_Track(next_id, det, frame_idx))
                    next_id += 1

            for ti, t in enumerate(active):
                if ti not in matched_tracks:
                    t.lost_count += 1
        else:
            for t in active:
                t.lost_count += 1
            for det in frame_dets:
                active.append(_Track(next_id, det, frame_idx))
                next_id += 1

        still_active: list[_Track] = []
        for t in active:
            if t.lost_count > max_lost_frames:
                closed.append(t)
            else:
                still_active.append(t)
        active = still_active

    closed.extend(active)
    return [t.finalise() for t in closed]


# ── Factory ───────────────────────────────────────────────────────────────────


def get_tracker(variant: str, **kwargs: Any) -> TrackerBase:
    """Return a tracker adapter for the given routing variant.

    Logic:
      - ``iou-tracker`` (or unknown / empty) → IoUTracker
      - ``sam3-mask-tracker``                → SAM3MaskTracker
      - ``botsort``                          → BoTSORTTracker (Issue #129, nightly)
      - ``strongsort``                       → StrongSORTTracker (Issue #129, nightly)
      - ``stub``                             → StubTracker

    The BoT-SORT / StrongSORT adapters live in ``pipeline.tracking`` and are
    imported lazily here so this module (and ``pytest`` collection) never
    depends on them at import time. They are pure-NumPy and carry no model
    weights, so the import cannot fail on a clean environment.
    """
    v = (variant or "").lower()
    if v == SAM3_MASK_TRACKER:
        return SAM3MaskTracker(**kwargs)
    if v == BOTSORT:
        from pipeline.tracking.botsort_adapter import BoTSORTTracker

        return BoTSORTTracker(**kwargs)
    if v == STRONGSORT:
        from pipeline.tracking.strongsort_adapter import StrongSORTTracker

        return StrongSORTTracker(**kwargs)
    if v == STUB_TRACKER:
        return StubTracker()
    if v and v != IOU_TRACKER:
        log.warning("unknown_tracker_variant_using_iou", variant=variant)
    return IoUTracker(**kwargs)
