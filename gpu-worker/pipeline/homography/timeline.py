"""Time-indexed field transforms for a moving, unsynchronised recording.

Only observed sample frames are mapped. Failed camera alignment creates a gap;
we never extend a representative matrix across an unobserved pan or camera cut.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.homography import camera_motion_ecc as ecc
from pipeline.homography import confidence_scorer as cs
from pipeline.runtime import check_control
from pipeline.video_timeline import VideoTimeline, iter_frames


def calibration_timeline(
    video_path: Path, fit_frame: Callable, *, threshold: float = 0.75,
    timeline: VideoTimeline | None = None,
    start_frame: int = 0, end_frame: int | None = None,
    anchor_seconds: float = 2.0, lost_retry_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Per-frame field transforms at the detection stride.

    ``start_frame``/``end_frame`` restrict the walk to one play's window of a
    long game film. Frame numbers stay those of the whole recording, so the
    series lines up with the detector's (``frame % stride == 0``) samples.

    Anchors are refitted every ``anchor_seconds``; while no transform is held
    (start of a play, after a zoom cut) every ``lost_retry_seconds`` instead,
    so a broadcast camera that zooms is re-acquired within a fraction of a
    second rather than losing the next two seconds of the play.
    """
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30)
    from pipeline.stage_detect import detection_frame_step

    stride = detection_frame_step(fps)
    timeline = timeline or VideoTimeline.from_path(
        video_path, fps_fallback=fps,
        frame_count_fallback=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
    )
    interval = max(stride, int(fps * anchor_seconds))
    lost_interval = max(stride, int(fps * (lost_retry_seconds or anchor_seconds)))
    calibrations: list[dict[str, Any]] = []
    provisional_samples: list[int] = []
    provisional_anchor = False
    anchor_provenance = "classical"
    previous_gray = None
    matrix = None
    confidence = 0.0
    anchor_frame = start_frame - interval
    previous_seconds = None
    try:
        # Frames by timestamp at the detector's stride, never by counting
        # (see video_timeline), so each transform pairs with its detections.
        for frame_number, frame in iter_frames(
            cap, timeline, start_frame, end_frame, wanted=lambda i: i % stride == 0, fill_gaps=False
        ):
            check_control()
            h, w = frame.shape[:2]
            scale = min(1.0, 384 / w)
            small = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))))
            gray = ecc.to_gray(small)
            reason = None
            if matrix is not None and previous_gray is not None:
                warp, aligned = ecc.estimate_warp(previous_gray, gray, max_iters=20)
                if aligned:
                    scaled = ecc.affine_to_homography(warp)
                    transform = np.diag([scale, scale, 1.0])
                    forward = np.linalg.inv(transform) @ scaled @ transform
                    elapsed = (timeline.timestamp(frame_number) - previous_seconds
                               if previous_seconds is not None else stride / fps)
                    if ecc.exceeds_zoom_rate(forward, frames_elapsed=1, fps=1 / max(elapsed, 1e-6)):
                        matrix = None
                        reason = "camera_zoom_discontinuity"
                    else:
                        # ECC maps previous pixels into current pixels. A field
                        # transform needs the inverse map back to the anchor.
                        matrix = matrix @ np.linalg.inv(forward)
                        confidence *= 0.999
                else:
                    matrix = None
                    reason = "camera_alignment_failed"
            # Re-fit at bounded intervals; do not run Hough on every lost frame.
            due = lost_interval if matrix is None else interval
            if frame_number - anchor_frame >= due:
                anchor_frame = frame_number
                fitted = fit_frame([frame])
                if fitted is not None:
                    candidate, keypoints, inliers, codes = fitted
                    score = (
                        keypoints.fit_confidence
                        if getattr(keypoints, "fit_confidence", None) is not None
                        else cs.compute_confidence(
                            inlier_ratio=inliers, line_count=keypoints.line_count,
                            parallel_line_score=cs.parallel_line_score(keypoints.yardline_angles),
                            temporal_stability=1.0, field_coverage=keypoints.field_coverage,
                        ).confidence
                    )
                    blocking = {"ambiguous_field_rows", "insufficient_lines", "low_inlier_ratio"}
                    if score >= threshold and not blocking.intersection(codes):
                        numeral_anchor = "numeral_anchored" in codes
                        if matrix is None:
                            matrix, confidence = candidate, score
                            provisional_anchor = not numeral_anchor
                            anchor_provenance = (
                                "numeral" if numeral_anchor else "classical_provisional"
                            )
                        else:
                            if numeral_anchor and provisional_anchor:
                                try:
                                    correction = candidate @ np.linalg.inv(matrix)
                                except np.linalg.LinAlgError:
                                    correction = None
                                if correction is not None:
                                    for index in provisional_samples:
                                        prior = np.asarray(
                                            calibrations[index]["homography"], dtype=float
                                        ).reshape(3, 3)
                                        reconciled = correction @ prior
                                        if abs(reconciled[2, 2]) > 1e-12:
                                            reconciled /= reconciled[2, 2]
                                        calibrations[index]["homography"] = reconciled.flatten().tolist()
                                        calibrations[index]["anchor_provenance"] = "numeral_reconciled"
                                        calibrations[index]["reason_codes"] = [
                                            *calibrations[index]["reason_codes"],
                                            "numeral_anchor_reconciled",
                                        ]
                                    matrix, confidence = candidate, score
                                    provisional_anchor = False
                                    anchor_provenance = "numeral"
                                    provisional_samples.clear()
                                else:
                                    reason = "anchor_reconciliation_failed"
                            else:
                                # Repeated yard lines can relabel the same location
                                # five yards away. Preserve the established frame.
                                corners = np.array([[w*.25, h*.25], [w*.75, h*.75]])
                                gap = ecc._mean_reprojection_gap(matrix, candidate, corners)
                                if gap <= 1.0:
                                    matrix, confidence = candidate, score
                                    if numeral_anchor:
                                        anchor_provenance = "numeral"
                                else:
                                    reason = "anchor_label_inconsistent"
                    elif matrix is None:
                        reason = "anchor_confidence_low"
            safe = matrix is not None and confidence >= threshold
            point = {
                "frame_number": frame_number,
                "timestamp_seconds": timeline.timestamp(frame_number),
                "homography": matrix.flatten().tolist() if safe else None,
                "analytics_safe": safe,
                "confidence": round(confidence, 4) if safe else 0.0,
                "reason_codes": [reason] if reason else ([] if safe else ["no_temporal_anchor"]),
            }
            if safe:
                point["anchor_provenance"] = anchor_provenance
                if provisional_anchor:
                    provisional_samples.append(len(calibrations))
            calibrations.append(point)
            previous_gray = gray
            previous_seconds = timeline.timestamp(frame_number)
    finally:
        cap.release()
    return calibrations
