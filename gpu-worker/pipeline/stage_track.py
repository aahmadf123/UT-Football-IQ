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

import math
from pathlib import Path
from typing import Any

import structlog

from pipeline import backend
from pipeline.homography.field_template import default_template
from pipeline.homography.project import (
    apply_homography_many,
    ground_anchor,
    homography_from_flat,
)
from pipeline.runtime import PipelineInterrupted, check_control
from pipeline.tracker_models import StubTracker, TrackerBase, get_tracker

log = structlog.get_logger(__name__)

MAX_LOST_SECONDS = 1.0
IOU_THRESHOLD = 0.3
# How far past the playing surface a real player can be, in yards. Deep enough
# to cover a receiver carried out of bounds and a defender chasing into the
# bench area, without admitting a field twice its true width.
MAX_OUT_OF_BOUNDS_YD = 15.0
#: Fraction trimmed from each end before measuring spread, so a few false-positive
#: tracks cannot veto a good calibration.
EXTENT_TAIL_FRACTION = 0.05


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
    calibration_series: list[dict[str, Any]] | None = None,
    capture_regime: str | None = None,
    detector_provenance: dict[str, Any] | None = None,
    video_path: Path | None = None,
    timeline: Any = None,
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
            max_lost_frames=max(1, round(fps * MAX_LOST_SECONDS)),
        )
    tracker.max_lost_seconds = MAX_LOST_SECONDS
    feature_diagnostics: dict[str, Any] = {}
    from pipeline.tracking.botsort_adapter import BoTSORTTracker
    if isinstance(tracker, BoTSORTTracker) and video_path is not None:
        from pipeline.tracking.frame_features import tracking_frame_features
        detections, camera_motion, feature_diagnostics = tracking_frame_features(
            video_path, detections, fps, timeline
        )
        tracker.camera_motion = camera_motion

    results = tracker.track(detections)
    # Track officials in a separate association pool. The player adapters
    # deliberately ignore nonplayers; previously this silently lost a referee
    # recognized by detection before the overlay could display it.
    official_detections = {
        frame: [{**det, "class": "player"} for det in frame_dets
                if det.get("class") == "official"]
        for frame, frame_dets in detections.items()
    }
    official_results = []
    if any(official_detections.values()):
        official_tracker = get_tracker(
            variant or "iou-tracker", iou_threshold=IOU_THRESHOLD,
            max_lost_frames=max(1, round(fps * MAX_LOST_SECONDS)),
        )
        official_tracker.max_lost_seconds = MAX_LOST_SECONDS
        official_results = official_tracker.track(official_detections)
    projected_points = _project_tracks(
        [*results, *official_results], homography, analytics_safe, calibration_series=calibration_series,
        capture_regime=capture_regime,
    )

    tracklet_ids: list[str] = []
    tracklets: list[dict[str, Any]] = []
    non_player_tracklets: list[dict[str, Any]] = []
    masked_tracklet_count = 0
    detector_provenance = detector_provenance or {}
    for track, actor_kind in [(t, "player") for t in results] + [
        (t, "official") for t in official_results
    ]:
        check_control()
        if len(track.points) < 2 and actor_kind == "player":
            continue
        # Tracking confidence = mean detection confidence over the track's
        # points. This is a TRACKING-quality signal (how sure the detector was
        # about the boxes this track is built from), never identity.
        confidences = [
            c
            for p in track.points
            if isinstance(c := p.get("detection_confidence"), (int, float))
        ]
        track_confidence = round(sum(confidences) / len(confidences), 4) if confidences else None
        synthetic = (
            isinstance(tracker, StubTracker)
            or detector_provenance.get("source") == "synthetic_test"
            or any(point.get("source") == "synthetic_test" for point in track.points)
        )
        source = "synthetic_test" if synthetic else detector_provenance.get("source")
        if not source:
            source = "real" if any(point.get("source") == "real" for point in track.points) else "unknown"
        provenance = {
            **detector_provenance,
            "source": source,
            "method": type(tracker).__name__,
            "actual_model": detector_provenance.get("effective_variant") or detector_provenance.get("actual_model"),
            # A detector benchmark does not independently validate track IDs.
            "validated": False,
            "detector_validated": not synthetic and detector_provenance.get("validated") is True,
            "shot_id": track.points[0].get("shot_id"),
            "source_start_seconds": track.points[0].get("timestamp_seconds"),
            "source_end_seconds": track.points[-1].get("timestamp_seconds"),
            "visibility_state": "observed",
            "max_lost_seconds": MAX_LOST_SECONDS,
            "tracker_inputs": feature_diagnostics,
            "actor_kind": actor_kind,
        }
        if actor_kind == "official":
            provenance["actor_classification"] = {
                "method": "detection_official_candidate", "validated": False,
                "status": "unreviewed",
            }
        try:
            resp = backend.create_tracklet(
                clip_id,
                start_frame=track.start_frame,
                end_frame=track.last_frame,
                track_points=track.points,
                track_confidence=track_confidence,
                team_label="official" if actor_kind == "official" else "unknown",
                job_id=job_id,
                provenance=provenance,
                model_version_id=detector_provenance.get("model_version_id"),
            )
            tracklet_ids.append(resp["id"])
            # Full tracklet record for in-process consumers (reid / pose /
            # events / labels / metrics take `tracklets` dicts in exactly
            # the backend row shape — carrying them here saves the
            # orchestrator a read-back round trip).
            (non_player_tracklets if actor_kind == "official" else tracklets).append(
                {
                    "id": resp["id"],
                    "clip_id": clip_id,
                    "start_frame": track.start_frame,
                    "end_frame": track.last_frame,
                    "track_points": track.points,
                    "track_confidence": track_confidence,
                    "team_label": "official" if actor_kind == "official" else "unknown",
                    "class": actor_kind,
                    "provenance": provenance,
                    "model_version_id": detector_provenance.get("model_version_id"),
                }
            )
            if any("mask" in p for p in track.points):
                masked_tracklet_count += 1
        except PipelineInterrupted:
            raise
        except Exception as exc:
            log.warning("tracklet_write_failed", track_id=track.track_id, error=str(exc))
            raise

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
        # These rows are persisted for overlays, but never handed to player
        # re-identification, formations, pose, metrics or opportunity analysis.
        "non_player_tracklets": non_player_tracklets,
        "official_tracklet_count": len(non_player_tracklets),
        "mask_aware": tracker.mask_aware,
        "projected_points": projected_points,
        "tracker_inputs": feature_diagnostics,
    }


def _project_tracks(
    results: list[Any],
    homography: list[float] | None,
    analytics_safe: bool,
    *,
    calibration_series: list[dict[str, Any]] | None = None,
    capture_regime: str | None = None,
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
        return 0
    if calibration_series:
        transforms = {int(p["frame_number"]): p for p in calibration_series}
        pending = []
        for track in results:
            for point in track.points:
                calibration = transforms.get(point.get("frame_number"))
                if not calibration or not calibration.get("analytics_safe") or not point.get("bbox"):
                    continue
                matrix = homography_from_flat(calibration.get("homography"))
                if matrix is not None:
                    xy = apply_homography_many(matrix, [ground_anchor(point["bbox"])])[0]
                    if all(math.isfinite(v) for v in xy):
                        pending.append((point, xy))
        if pending and _extent_is_possible([xy for _, xy in pending]):
            for point, (fx, fy) in pending:
                point["field_x"], point["field_y"] = fx, fy
            return len(pending)
        return 0
    if capture_regime in {"drone_follow", "unconstrained", "unknown"}:
        return 0
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


def _spread(values: list[float]) -> float:
    """Range of ``values`` ignoring the outermost ``EXTENT_TAIL_FRACTION`` each end.

    Min-to-max would let a single bad track speak for the whole clip. Detectors
    do produce false positives on field markings and sideline furniture, and
    those sit at the edges of the frame -- exactly where projection error is
    largest -- so the raw range is the one statistic guaranteed to be dominated
    by the least trustworthy points.
    """
    ordered = sorted(values)
    if len(ordered) < 20:  # too few to spare any; use the full range
        return ordered[-1] - ordered[0]
    cut = int(len(ordered) * EXTENT_TAIL_FRACTION)
    return ordered[-1 - cut] - ordered[cut]


def _extent_is_possible(points: list[tuple[float, float]]) -> bool:
    """Could these projected positions be players on a football field?

    A tolerance, not a boundary test: players legitimately stand out of bounds,
    behind the end line, and on the sideline. This only rejects a spread that no
    football field could contain -- the signature of a homography whose scale is
    wrong on one axis, which fits its own correspondences perfectly and reports
    a clean inlier ratio while placing players tens of yards from where they are.
    """
    tmpl = default_template()
    # Generous: the full width plus a first down of room on each side, and the
    # full length plus both end zones and then some.
    return (
        _spread([y for _, y in points]) <= tmpl.width + 2 * MAX_OUT_OF_BOUNDS_YD
        and _spread([x for x, _ in points]) <= tmpl.length + 2 * MAX_OUT_OF_BOUNDS_YD
    )
