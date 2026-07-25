"""BoT-SORT tracker adapter (Issue #129).

BoT-SORT augments a ByteTrack-style IoU association with two ideas that
matter for football film:

  1. **Camera-motion compensation (CMC).** A constant-velocity motion model
     predicts where each track's box should land in the next frame, and a
     per-frame global affine warp (from the ECC stage,
     ``pipeline.homography.camera_motion_ecc``) cancels the operator pan/zoom
     of the ``drone_follow`` regime before scoring. This is what lets the
     tracker survive a hard pan that pure-IoU loses.
  2. **Appearance ReID (optional).** When a detection already carries an
     appearance ``embedding`` (or an ``appearance_fn`` is supplied), a cosine
     term is blended into the association score to disambiguate the
     same-uniform problem.

Both extras are *optional injected inputs*. With neither supplied the adapter
degrades to a constant-velocity, IoU-scored tracker — strictly no worse than
``IoUTracker`` on static-camera, low-velocity clips and better under pan. This
keeps the adapter inside the existing ``TrackerBase`` contract (it only ever
receives ``detections_by_frame``) and fully testable without a GPU, video
frames, or any model weights.

Routing: BoT-SORT is **nightly-only** (``NIGHTLY_ONLY_VARIANTS``); it is
selected for the nightly ``track`` bucket when ``ENABLE_BOTSORT_NIGHTLY=1``.
It never runs on the same-session path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import structlog

from pipeline.tracker_models import (
    TrackerBase,
    TrackResult,
    _greedy_assign,
    _Track,
    bbox_iou,
)

log = structlog.get_logger(__name__)


def _bbox_center(bbox: list[float]) -> tuple[float, float]:
    return (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0


def warp_bbox(bbox: list[float], warp: np.ndarray | None) -> list[float]:
    """Apply a 2×3 affine ``warp`` to an axis-aligned ``bbox``.

    The four corners are transformed and re-bounded into a new axis-aligned
    box. A ``None``/identity warp returns the bbox unchanged.
    """
    if warp is None:
        return list(bbox)
    x1, y1, x2, y2 = bbox
    corners = np.array(
        [[x1, y1, 1.0], [x2, y1, 1.0], [x2, y2, 1.0], [x1, y2, 1.0]],
        dtype=np.float64,
    )
    m = np.asarray(warp, dtype=np.float64).reshape(2, 3)
    pts = corners @ m.T  # (4, 2)
    nx1, ny1 = float(pts[:, 0].min()), float(pts[:, 1].min())
    nx2, ny2 = float(pts[:, 0].max()), float(pts[:, 1].max())
    return [nx1, ny1, nx2, ny2]


class _MotionTrack:
    """Internal track state with a constant-velocity center model."""

    __slots__ = (
        "track_id",
        "bbox",
        "velocity",
        "mask",
        "embedding",
        "start_frame",
        "last_frame",
        "lost_count",
        "points",
    )

    def __init__(
        self,
        track_id: int,
        det: dict[str, Any],
        frame: int,
        embedding: np.ndarray | None = None,
    ) -> None:
        self.track_id = track_id
        self.bbox: list[float] = list(det["bbox"])
        self.velocity: tuple[float, float] = (0.0, 0.0)
        self.mask: dict[str, Any] | None = det.get("mask")
        self.embedding: np.ndarray | None = (
            embedding if embedding is not None else _as_embedding(det.get("embedding"))
        )
        self.start_frame = frame
        self.last_frame = frame
        self.lost_count = 0
        # Reuse the canonical track-point schema builder so the output is
        # byte-for-byte identical to the IoU / SAM3 trackers.
        self.points: list[dict[str, Any]] = [_Track._point(det, frame)]

    def predict(self, frame: int, warp: np.ndarray | None) -> list[float]:
        """Predicted bbox at ``frame``.

        First camera-compensate the last box into the current frame's
        coordinates (``warp``), *then* add the residual world velocity. Doing
        it in this order avoids double-counting the camera pan: ``velocity`` is
        the motion that remains after camera motion is removed.
        """
        compensated = warp_bbox(self.bbox, warp) if warp is not None else list(self.bbox)
        dt = max(0, frame - self.last_frame)
        vx, vy = self.velocity
        return [
            compensated[0] + vx * dt,
            compensated[1] + vy * dt,
            compensated[2] + vx * dt,
            compensated[3] + vy * dt,
        ]

    def update(
        self,
        det: dict[str, Any],
        frame: int,
        warp: np.ndarray | None,
        embedding: np.ndarray | None = None,
    ) -> None:
        new_bbox = list(det["bbox"])
        # Measure velocity as the residual after camera compensation, so a
        # world-static player under a panning camera converges to ~zero.
        compensated = warp_bbox(self.bbox, warp) if warp is not None else list(self.bbox)
        ocx, ocy = _bbox_center(compensated)
        ncx, ncy = _bbox_center(new_bbox)
        dt = max(1, frame - self.last_frame)
        # Exponential blend keeps a single ID-swap from snapping the velocity.
        nvx, nvy = (ncx - ocx) / dt, (ncy - ocy) / dt
        ovx, ovy = self.velocity
        self.velocity = (0.5 * ovx + 0.5 * nvx, 0.5 * ovy + 0.5 * nvy)
        self.bbox = new_bbox
        self.mask = det.get("mask", self.mask)
        emb = embedding if embedding is not None else _as_embedding(det.get("embedding"))
        if emb is not None:
            self.embedding = emb if self.embedding is None else _ema(self.embedding, emb)
        self.last_frame = frame
        self.lost_count = 0
        self.points.append(_Track._point(det, frame))

    def finalise(self) -> TrackResult:
        return TrackResult(
            track_id=self.track_id,
            start_frame=self.start_frame,
            last_frame=self.last_frame,
            points=self.points,
        )


def _as_embedding(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    arr = np.asarray(value, dtype=np.float64).ravel()
    if arr.size == 0:
        return None
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 1e-6 else arr


def _ema(old: np.ndarray, new: np.ndarray, alpha: float = 0.9) -> np.ndarray:
    blended = alpha * old + (1.0 - alpha) * new
    norm = float(np.linalg.norm(blended))
    return blended / norm if norm > 1e-6 else blended


def _cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


class BoTSORTTracker(TrackerBase):
    """Motion- and (optionally) appearance-aware tracker.

    Args:
        iou_threshold: minimum combined score to keep an association.
        max_lost_frames: frames a track may coast before it is closed.
        appearance_weight: ∈ [0, 1]; weight of the cosine-appearance term
            relative to motion IoU. Only contributes when both the track and
            the detection expose an ``embedding`` (or ``appearance_fn`` yields
            one). Defaults to 0.25.
        camera_motion: optional ``{frame_idx: 2x3 affine}`` mapping that warps
            coordinates from the previous processed frame into ``frame_idx``
            (i.e. the ECC global-motion estimate). When supplied, predicted
            boxes are camera-compensated before scoring. ``None`` disables CMC.
        appearance_fn: optional callable ``det -> embedding`` used when a
            detection does not already carry an ``embedding`` key.
    """

    mask_aware = False

    def __init__(
        self,
        iou_threshold: float = 0.3,
        max_lost_frames: int = 30,
        appearance_weight: float = 0.25,
        camera_motion: dict[int, np.ndarray] | None = None,
        appearance_fn: Callable[[dict[str, Any]], np.ndarray] | None = None,
    ) -> None:
        self.iou_threshold = iou_threshold
        self.max_lost_frames = max_lost_frames
        self.appearance_weight = max(0.0, min(1.0, appearance_weight))
        self.camera_motion = camera_motion or {}
        self.appearance_fn = appearance_fn

    def _chained_warp(self, last_frame: int, frame: int) -> np.ndarray | None:
        """Compose per-frame affine warps over ``(last_frame, frame]``.

        Missing per-frame warps are treated as identity (skipped), so a sparse
        ``camera_motion`` map degrades gracefully rather than dropping tracks.
        """
        if not self.camera_motion or frame <= last_frame:
            return None
        composed: np.ndarray | None = None
        for f in range(last_frame + 1, frame + 1):
            w = self.camera_motion.get(f)
            if w is None:
                continue
            m = np.asarray(w, dtype=np.float64).reshape(2, 3)
            full = np.vstack([m, [0.0, 0.0, 1.0]])
            composed = full if composed is None else full @ composed
        if composed is None:
            return None
        return composed[:2, :]

    def _embedding_for(self, det: dict[str, Any]) -> np.ndarray | None:
        emb = _as_embedding(det.get("embedding"))
        if emb is not None or self.appearance_fn is None:
            return emb
        try:
            return _as_embedding(self.appearance_fn(det))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("botsort_appearance_fn_failed", error=str(exc))
            return None

    def _score(
        self,
        track: _MotionTrack,
        det: dict[str, Any],
        frame: int,
        det_emb: np.ndarray | None = None,
    ) -> float:
        warp = self._chained_warp(track.last_frame, frame)
        predicted = track.predict(frame, warp)
        iou = bbox_iou(predicted, det["bbox"])
        if self.appearance_weight <= 0.0:
            return iou
        if det_emb is None:
            det_emb = self._embedding_for(det)
        if track.embedding is None or det_emb is None:
            return iou
        appearance = max(0.0, _cosine(track.embedding, det_emb))
        return (1.0 - self.appearance_weight) * iou + self.appearance_weight * appearance

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
            frame_embeddings = [self._embedding_for(d) for d in frame_dets]

            if active and frame_dets:
                cost = [
                    [
                        self._score(t, d, frame_idx, frame_embeddings[di])
                        for di, d in enumerate(frame_dets)
                    ]
                    for t in active
                ]
                matches = _greedy_assign(cost, self.iou_threshold)
                matched_tracks = {m[0] for m in matches}
                matched_dets = {m[1] for m in matches}

                for ti, di in matches:
                    warp = self._chained_warp(active[ti].last_frame, frame_idx)
                    active[ti].update(
                        frame_dets[di],
                        frame_idx,
                        warp,
                        embedding=frame_embeddings[di],
                    )

                for di, det in enumerate(frame_dets):
                    if di not in matched_dets:
                        active.append(
                            _MotionTrack(
                                next_id,
                                det,
                                frame_idx,
                                embedding=frame_embeddings[di],
                            )
                        )
                        next_id += 1

                for ti, t in enumerate(active):
                    if ti not in matched_tracks:
                        t.lost_count += 1
            else:
                for t in active:
                    t.lost_count += 1
                for di, det in enumerate(frame_dets):
                    active.append(
                        _MotionTrack(
                            next_id,
                            det,
                            frame_idx,
                            embedding=frame_embeddings[di],
                        )
                    )
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
