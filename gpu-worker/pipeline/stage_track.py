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
from pipeline.homography.field_template import default_template
from pipeline.homography.project import (
    apply_homography_many,
    ground_anchor,
    homography_from_flat,
)
from pipeline.tracker_models import TrackerBase, get_tracker

log = structlog.get_logger(__name__)

MAX_LOST_FRAMES = 30  # frames a track can be "lost" before it is closed
IOU_THRESHOLD = 0.3
# How far past the playing surface a real player can be, in yards. Deep enough
# to cover a receiver carried out of bounds and a defender chasing into the
# bench area, without admitting a field twice its true width.
MAX_OUT_OF_BOUNDS_YD = 15.0


def run(
    clip_id: str,
    detections: dict[str, list[dict[str, Any]]],
    fps: float,
    job_id: str,
    *,
    tracker: TrackerBase | None = None,
    variant: str | None = None,
    homography: list[float] | None = None,
    analytics_safe: bool = False,
) -> dict[str, Any]:
    """Run tracking and write tracklets to the backend.

    Args:
        clip_id, detections, fps, job_id: pipeline-standard arguments.
        tracker:  Pre-built tracker adapter; overrides ``variant``.
        variant:  Routing variant id (e.g. ``"iou-tracker"``,
                  ``"sam3-mask-tracker"``).  Defaults to ``"iou-tracker"``.
        homography: Flat 9-element pixel→yard matrix from the calibrate stage.
        analytics_safe: Whether that calibration cleared its confidence gate.
            Coordinates are only written when it did — a homography nobody
            trusts produces plausible-looking yard values that are wrong, which
            is worse than none at all.
    """
    log.info("stage_track_start", clip_id=clip_id, variant=variant)

    if tracker is None:
        tracker = get_tracker(
            variant or "iou-tracker",
            iou_threshold=IOU_THRESHOLD,
            max_lost_frames=MAX_LOST_FRAMES,
        )

    results = tracker.track(detections)
    projected_points = _project_tracks(results, homography, analytics_safe)

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
            log.warning("tracklet_write_failed", track_id=track.track_id, error=str(exc))

    log.info(
        "stage_track_done",
        clip_id=clip_id,
        tracklet_count=len(tracklet_ids),
        masked_tracklet_count=masked_tracklet_count,
        mask_aware=tracker.mask_aware,
        projected_points=projected_points,
    )
    return {
        "tracklet_count": len(tracklet_ids),
        "tracklet_ids": tracklet_ids,
        "tracklets": tracklets,
        "mask_aware": tracker.mask_aware,
        "projected_points": projected_points,
    }


def _project_tracks(
    results: list[Any],
    homography: list[float] | None,
    analytics_safe: bool,
) -> int:
    """Attach field coordinates to every track point, in place.

    This is the step whose absence made ``field_x`` a column that a dozen
    modules read and none wrote. Mutating the point dicts here means both the
    backend write (``create_tracklet(track_points=track.points)``) and the
    in-memory tracklets handed to later stages pick the coordinates up with no
    further plumbing.

    The projected point is the detection's **ground anchor** -- bottom-centre of
    the box, i.e. the player's feet. A homography describes the ground plane, so
    projecting the box centre would place every player a yard or two downfield,
    consistently and undetectably.

    Returns the number of points projected, for the stage log.
    """
    if not analytics_safe:
        # Not an error. Film with no visible yard lines still tracks, renders,
        # and produces clips; it just cannot produce spatial metrics, and the
        # readers all guard on `field_x is not None`.
        return 0

    matrix = homography_from_flat(homography)
    if matrix is None:
        log.warning("stage_track_homography_unusable", analytics_safe=analytics_safe)
        return 0

    pending: list[tuple[dict[str, Any], tuple[float, float]]] = []
    for track in results:
        points = [p for p in track.points if p.get("bbox")]
        if not points:
            continue
        field_xy = apply_homography_many(matrix, [ground_anchor(p["bbox"]) for p in points])
        pending.extend(zip(points, field_xy, strict=True))

    if not pending:
        return 0

    if not _extent_is_possible([xy for _, xy in pending]):
        # Written nowhere rather than written wrong. A homography can be exactly
        # right along one axis and badly scaled along the other -- that is what
        # happens when the two detected field rows are assumed to be the hashes
        # and are really a hash and a sideline -- and the fit reports a perfect
        # inlier ratio either way, because it is consistent with its own
        # correspondences. Players standing 40 yards out of bounds is the only
        # signal that survives, so it is the one checked here.
        return 0

    for point, (fx, fy) in pending:
        point["field_x"] = fx
        point["field_y"] = fy
    return len(pending)


def _extent_is_possible(points: list[tuple[float, float]]) -> bool:
    """Could these projected positions be players on a football field?

    A tolerance, not a boundary test: players legitimately stand out of bounds,
    behind the end line, and on the sideline. This only rejects a spread that no
    football field could contain -- the signature of a homography whose scale is
    wrong on one axis.
    """
    tmpl = default_template()
    ys = [y for _, y in points]
    xs = [x for x, _ in points]
    # Generous: the full width plus a first down of room on each side, and the
    # full length plus both end zones and then some.
    return (
        max(ys) - min(ys) <= tmpl.width + 2 * MAX_OUT_OF_BOUNDS_YD
        and max(xs) - min(xs) <= tmpl.length + 2 * MAX_OUT_OF_BOUNDS_YD
    )
