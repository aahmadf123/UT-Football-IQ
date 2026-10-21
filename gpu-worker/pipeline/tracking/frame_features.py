"""Measured image inputs for optional motion/appearance tracker baselines."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from pipeline.homography.camera_motion_ecc import background_mask, estimate_warp
from pipeline.runtime import check_control
from pipeline.video_timeline import VideoTimeline, iter_frames


def torso_histogram(frame: np.ndarray, bbox: list[float]) -> list[float] | None:
    """Actual torso color descriptor; this is not a trained athlete identifier."""
    x0, y0, x1, y1 = bbox
    height, width = frame.shape[:2]
    torso = frame[max(0, round(y0 + 0.2 * (y1 - y0))):min(height, round(y0 + 0.7 * (y1 - y0))),
                  max(0, round(x0)):min(width, round(x1))]
    if not torso.size:
        return None
    hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).ravel()
    norm = float(np.linalg.norm(histogram))
    return (histogram / norm).astype(float).tolist() if norm > 0 else None


def tracking_frame_features(source: Path, detections: dict[str, list[dict[str, Any]]],
                            fps: float, timeline: VideoTimeline | None = None
                            ) -> tuple[dict[str, list[dict[str, Any]]], dict[int, np.ndarray], dict[str, Any]]:
    """Decode the detection sample once, extracting appearance and camera motion."""
    from pipeline.hwaccel import nvdec_video_capture

    enriched = {key: [dict(d) for d in values] for key, values in detections.items()}
    wanted = {int(key) for key in detections}
    diagnostics = {"appearance_method": "torso_hsv_histogram_baseline",
                   "camera_motion_method": "background_masked_ecc",
                   "appearance_observations": 0, "camera_pairs_measured": 0,
                   "camera_pairs_unavailable": 0, "validated": False}
    if not wanted:
        return enriched, {}, diagnostics
    timeline = timeline or VideoTimeline.from_path(source, fps_fallback=fps)
    capture = nvdec_video_capture(source)
    if not capture.isOpened():
        capture.release()
        diagnostics["reason"] = "source_decode_unavailable"
        return enriched, {}, diagnostics
    previous = None
    previous_shot = None
    previous_mask = None
    warps = {}
    try:
        for frame_number, frame in iter_frames(capture, timeline, min(wanted), max(wanted) + 1,
                                               wanted=lambda index: index in wanted, fill_gaps=False):
            check_control()
            values = enriched.get(str(frame_number), [])
            for detection in values:
                if detection.get("class", "player") == "player" and detection.get("bbox"):
                    feature = torso_histogram(frame, detection["bbox"])
                    if feature is not None:
                        detection["embedding"] = feature
                        diagnostics["appearance_observations"] += 1
            scale = min(1, 480 / frame.shape[1])
            small = cv2.resize(frame, (max(1, round(frame.shape[1] * scale)),
                                       max(1, round(frame.shape[0] * scale))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            grass = cv2.inRange(hsv, (25, 35, 30), (95, 255, 255))
            # Include painted lines adjacent to grass, then remove moving people.
            grass = cv2.dilate(grass, np.ones((9, 9), dtype=np.uint8))
            boxes = [tuple(round(v * scale) for v in detection["bbox"]) for detection in values
                     if detection.get("bbox")]
            mask = background_mask(grass, boxes)
            shot = next((d.get("shot_id") for d in values if d.get("shot_id") is not None), None)
            if previous is not None and previous.shape == gray.shape and shot == previous_shot:
                # Phase correlation supplies a measured coarse translation;
                # ECC then refines it against static background. Starting every
                # pan at identity can miss the local ECC convergence basin.
                initial = np.eye(2, 3, dtype=np.float32)
                weights = (previous_mask > 0).astype(np.float32)
                shift, response = cv2.phaseCorrelate(previous.astype(np.float32) * weights,
                                                     gray.astype(np.float32) * weights)
                if (np.isfinite([*shift, response]).all() and response > 0.1
                        and abs(shift[0]) < gray.shape[1] * 0.4 and abs(shift[1]) < gray.shape[0] * 0.4):
                    initial[:, 2] = shift
                warp, success = estimate_warp(previous, gray, mask=previous_mask, initial_warp=initial)
                if success and np.isfinite(warp).all() and 0.5 < abs(np.linalg.det(warp[:, :2])) < 2:
                    transform = np.diag([small.shape[1] / frame.shape[1], small.shape[0] / frame.shape[0], 1])
                    full = np.vstack([warp, [0, 0, 1]])
                    warps[frame_number] = (np.linalg.inv(transform) @ full @ transform)[:2]
                    diagnostics["camera_pairs_measured"] += 1
                else:
                    diagnostics["camera_pairs_unavailable"] += 1
            previous, previous_mask, previous_shot = gray, mask, shot
    finally:
        capture.release()
    return enriched, warps, diagnostics
