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
from pipeline.video_timeline import VideoTimeline


def calibration_timeline(
    video_path: Path, fit_frame: Callable, *, threshold: float = 0.75,
    timeline: VideoTimeline | None = None,
) -> list[dict[str, Any]]:
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
    interval = max(stride, int(fps * 2))
    calibrations: list[dict[str, Any]] = []
    previous_gray = None
    matrix = None
    confidence = 0.0
    frame_number = 0
    anchor_frame = -interval
    previous_seconds = None
    try:
        while True:
            check_control()
            ok, frame = cap.read()
            if not ok:
                break
            if frame_number % stride:
                frame_number += 1
                continue
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
            if frame_number - anchor_frame >= interval:
                anchor_frame = frame_number
                fitted = fit_frame([frame])
                if fitted is not None:
                    candidate, keypoints, inliers, codes = fitted
                    score = cs.compute_confidence(
                        inlier_ratio=inliers, line_count=keypoints.line_count,
                        parallel_line_score=cs.parallel_line_score(keypoints.yardline_angles),
                        temporal_stability=1.0, field_coverage=keypoints.field_coverage,
                    ).confidence
                    blocking = {"ambiguous_field_rows", "insufficient_lines", "low_inlier_ratio"}
                    if score >= threshold and not blocking.intersection(codes):
                        if matrix is None:
                            matrix, confidence = candidate, score
                        else:
                            # Repeated yard lines can relabel the same location
                            # five yards away. Preserve the established frame.
                            corners = np.array([[w*.25, h*.25], [w*.75, h*.75]])
                            gap = ecc._mean_reprojection_gap(matrix, candidate, corners)
                            if gap <= 1.0:
                                matrix, confidence = candidate, score
                            else:
                                reason = "anchor_label_inconsistent"
                    elif matrix is None:
                        reason = "anchor_confidence_low"
            safe = matrix is not None and confidence >= threshold
            calibrations.append({
                "frame_number": frame_number,
                "timestamp_seconds": timeline.timestamp(frame_number),
                "homography": matrix.flatten().tolist() if safe else None,
                "analytics_safe": safe,
                "confidence": round(confidence, 4) if safe else 0.0,
                "reason_codes": [reason] if reason else ([] if safe else ["no_temporal_anchor"]),
            })
            previous_gray = gray
            previous_seconds = timeline.timestamp(frame_number)
            frame_number += 1
    finally:
        cap.release()
    return calibrations
