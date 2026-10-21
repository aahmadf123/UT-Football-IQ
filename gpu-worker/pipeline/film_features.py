"""Streaming, measured visual signals for format-aware segmentation proposals.

No learned football/replay classifier is implied. Native PTS address every
observation; motion after a stable period is a motion onset, not a proven snap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from pipeline.runtime import check_control
from pipeline.video_timeline import VideoTimeline, iter_frames


@dataclass(frozen=True)
class FeatureConfig:
    sample_hz: float = 10.
    size: tuple[int, int] = (256, 144)
    stable_seconds: float = .6
    settled_seconds: float = 1.
    minimum_action_seconds: float = 1.
    minimum_quiet_motion: float = 2.5  # resized pixels per source second
    minimum_active_motion: float = 7.


def image_fingerprint(frame: np.ndarray) -> str:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    tiny = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (tiny[:, 1:] > tiny[:, :-1]).ravel()
    return f"{sum(int(value) << index for index, value in enumerate(bits)):016x}"


def camera_residual(previous: np.ndarray, current: np.ndarray, delta: float) -> dict[str, Any]:
    """Fit background affine flow, then measure actual residual motion."""
    if delta <= 0:
        return {"motion": None, "active_fraction": 0., "camera_fit": False}
    before = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    after = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    flow = cv2.calcOpticalFlowFarneback(before, after, None, .5, 3, 15, 3, 5, 1.2, 0)
    hsv = cv2.cvtColor(previous, cv2.COLOR_BGR2HSV)
    grass = cv2.inRange(hsv, (25, 35, 30), (95, 255, 255))
    height, width = before.shape
    yy, xx = np.mgrid[4:height:8, 4:width:8]
    origins = np.column_stack([xx.ravel(), yy.ravel()]).astype(np.float32)
    vectors = flow[yy, xx].reshape(-1, 2)
    mask = grass[yy, xx].ravel() > 0
    if np.count_nonzero(mask) < 8:
        return {"motion": None, "active_fraction": 0., "camera_fit": False}
    affine, inliers = cv2.estimateAffinePartial2D(origins[mask], (origins + vectors)[mask],
                                                method=cv2.RANSAC, ransacReprojThreshold=1.)
    if affine is None or inliers is None or float(inliers.mean()) < .5 or not np.isfinite(affine).all():
        return {"motion": None, "active_fraction": 0., "camera_fit": False}
    full_y, full_x = np.mgrid[:height, :width]
    background_x = affine[0, 0]*full_x + affine[0, 1]*full_y + affine[0, 2] - full_x
    background_y = affine[1, 0]*full_x + affine[1, 1]*full_y + affine[1, 2] - full_y
    residual = np.hypot(flow[..., 0] - background_x, flow[..., 1] - background_y) / delta
    field = cv2.dilate(grass, np.ones((11, 11), np.uint8)) > 0
    values = residual[field]
    if not values.size:
        return {"motion": None, "active_fraction": 0., "camera_fit": False}
    return {"motion": float(np.percentile(values, 95)),
            "active_fraction": float(np.mean(values > 7.)), "camera_fit": True,
            "camera_affine": affine.round(6).tolist(), "fit_inlier_fraction": float(inliers.mean())}


def motion_events(samples: list[dict[str, Any]], config: FeatureConfig | None = None) -> list[dict[str, Any]]:
    """Setup → collective motion candidate → settlement, on actual PTS."""
    cfg = config or FeatureConfig()
    if cfg.sample_hz <= 0:
        raise ValueError("Source sampling frequency must be positive")
    events: list[dict[str, Any]] = []
    quiet_since, active_since, settled_since = None, None, None
    for sample in samples:
        stamp, motion = sample["timestamp_seconds"], sample.get("motion")
        if motion is None or not sample.get("camera_fit") or sample.get("scene_kind") != "field":
            quiet_since, settled_since = None, None
            continue
        if motion <= cfg.minimum_quiet_motion:
            quiet_since = stamp if quiet_since is None else quiet_since
            if active_since is not None and stamp - active_since >= cfg.minimum_action_seconds:
                settled_since = stamp if settled_since is None else settled_since
                if stamp - settled_since >= cfg.settled_seconds:
                    events.append({"event_type": "motion_settled", "timestamp_seconds": settled_since,
                                   "source_frame": next(s["source_frame"] for s in samples if s["timestamp_seconds"] == settled_since),
                                   "source": "camera_compensated_flow", "validated": False,
                                   "confidence": .5, "timestamp_verified": sample["timestamp_verified"]})
                    active_since, settled_since = None, None
            continue
        stable = quiet_since is not None and stamp - quiet_since >= cfg.stable_seconds
        if active_since is None and stable and motion >= cfg.minimum_active_motion and sample.get("active_fraction", 0) >= .04:
            active_since = stamp
            events.append({"event_type": "motion_onset", "timestamp_seconds": stamp,
                           "source_frame": sample["source_frame"], "source": "camera_compensated_flow",
                           "validated": False, "confidence": .5,
                           "timestamp_verified": sample["timestamp_verified"],
                           "signals": {"stable_seconds": stamp - quiet_since, "residual_motion": motion,
                                       "active_fraction": sample["active_fraction"]}})
        quiet_since, settled_since = None, None
    return events


def measured_view(frame: np.ndarray, green: float) -> tuple[str, float]:
    """Require actual field-line support; zero lines cannot imply endzone."""
    from pipeline.game_film import view_label

    if green < .35:
        return "unknown", 0.
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    white = cv2.inRange(hsv, (0, 0, 170), (180, 65, 255))
    lines = cv2.HoughLinesP(white, 1, np.pi / 180, 20, minLineLength=25, maxLineGap=8)
    if lines is None or len(lines) < 3:
        return "unknown", 0.
    steep = set()
    for x0, y0, x1, y1 in lines[:, 0]:
        angle = abs(np.degrees(np.arctan2(y1-y0, x1-x0)))
        angle = 180-angle if angle > 90 else angle
        if angle > 35:
            steep.add(round((x0+x1) / max(frame.shape[1], 1) * 12))
    return view_label(len(steep))


def extract_shot_evidence(video_path: Path, *, timeline: VideoTimeline | None = None,
                          config: FeatureConfig | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Single bounded-image decode pass; return only compact source observations."""
    from pipeline.game_film import _green_fraction
    from pipeline.hwaccel import nvdec_video_capture

    cfg = config or FeatureConfig()
    timeline = timeline or VideoTimeline.from_path(video_path)
    if not timeline.timestamps:
        raise ValueError("No source presentation timestamps available for shot audit")
    # Deduplicate displayed native frames when the source rate is below sample_hz.
    wanted = {timeline.frame_for_time(i / cfg.sample_hz) for i in range(int(timeline.duration * cfg.sample_hz) + 1)}
    capture = nvdec_video_capture(video_path)
    if not capture.isOpened():
        capture.release()
        raise ValueError("Source cannot be decoded for shot/event analysis")
    shots: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    previous, previous_hist, previous_hash = None, None, None
    feature_previous, previous_stamp = None, None
    start = 0.
    count = 0
    decoded_count = 0

    def finish(end: float) -> None:
        if not samples or end <= start:
            return
        green = float(np.median([sample["green_fraction"] for sample in samples]))
        votes = [sample["view"] for sample in samples if sample["view"] != "unknown"]
        view = max(set(votes), key=lambda label: (votes.count(label), label)) if votes else "unknown"
        confidence = votes.count(view) / len(samples) if votes else 0.
        sparse = samples[::max(1, len(samples) // 48)][:64]
        shot = {"id": f"shot-{samples[0]['source_frame']}", "start_seconds": start, "end_seconds": end,
                "source_start_frame": samples[0]["source_frame"], "source_end_frame": samples[-1]["source_frame"],
                "green_fraction": green, "view": view, "view_confidence": confidence,
                "scene_kind": "field" if green >= .35 else "non_field",
                "timestamp_verified": timeline.verified,
                "moving_samples": sum(s.get("motion") is not None and s["motion"] >= cfg.minimum_active_motion for s in samples),
                "fingerprints": [{"timestamp_seconds": s["timestamp_seconds"], "source_frame": s["source_frame"],
                                  "hash": s["image_hash"]} for s in sparse],
                "events": motion_events(samples, cfg),
                "feature_provenance": {"source": "real_decoded_pixels", "validated": False,
                                       "method": "field_color_affine_residual_motion_dhash", "sample_hz": cfg.sample_hz,
                                       "camera_fit_pairs": sum(s.get("camera_fit", False) for s in samples)}}
        shots.append(shot)

    try:
        for frame_number, frame in iter_frames(capture, timeline, 0, len(timeline.timestamps),
                                               fill_gaps=False):
            check_control()
            decoded_count += 1
            stamp = timeline.timestamp(frame_number)
            small = cv2.resize(frame, cfg.size, interpolation=cv2.INTER_AREA)
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            histogram = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256]).ravel()
            histogram /= histogram.sum() + 1e-9
            green = _green_fraction(hsv)
            digest = image_fingerprint(small)
            cut = False
            # Camera resets are independent of motion sampling and have no
            # minimum shot duration. Short cutaways still invalidate tracking.
            if previous is not None:
                difference = float(np.abs(histogram - previous_hist).sum())
                hash_change = (int(digest, 16) ^ int(previous_hash, 16)).bit_count() / 64
                image_change = float(np.mean(np.abs(small.astype(float) - previous))) / 255
                cut = difference > .55 or (hash_change > .38 and image_change > .22)
            if cut:
                finish(stamp)
                samples = []
                start = stamp
            previous, previous_hist, previous_hash = small, histogram, digest
            if frame_number not in wanted and not cut and samples:
                continue
            view, view_confidence = measured_view(small, green)
            movement = camera_residual(feature_previous, small, stamp - previous_stamp) if feature_previous is not None and not cut else {
                "motion": None, "active_fraction": 0., "camera_fit": False}
            samples.append({"source_frame": frame_number, "timestamp_seconds": stamp,
                            "timestamp_verified": timeline.verified, "green_fraction": green,
                            "image_hash": digest, "scene_kind": "field" if green >= .35 else "non_field",
                            "view": view, "view_confidence": view_confidence, **movement})
            feature_previous, previous_stamp = small, stamp
            count += 1
        finish(timeline.duration)
    finally:
        capture.release()
    if not shots:
        raise ValueError("Shot audit decoded no source observations")
    return shots, {"source": "real_decoded_pixels", "validated": False, "sample_count": count,
                   "decoded_frame_count": decoded_count, "cut_audit": "every_decoded_native_frame",
                   "timestamp_verified": timeline.verified, "source_pts_origin": timeline.pts_origin,
                   "sample_hz": cfg.sample_hz, "native_pts": timeline.verified,
                   "limitations": ["Motion onset is not a qualified football snap", "Scene subtypes need independent evidence"]}


def propose_visual_replays(shots: list[dict[str, Any]]) -> None:
    """Find an ordered, changing image sequence repeated in recent footage.

    Static field views cannot match; at least four distinct image hashes and a
    source-time span of one second are required. Results remain proposals.
    """
    for index, shot in enumerate(shots):
        existing = shot.get("replay_evidence") or {}
        if existing.get("target_shot_id") or (not shot.get("events") and shot.get("moving_samples", 0) < 3):
            continue
        current = shot.get("fingerprints", [])
        if len({sample["hash"] for sample in current}) < 4:
            continue
        candidates = []
        for prior in shots[max(0, index-12):index]:
            if (not prior.get("events") and prior.get("moving_samples", 0) < 3) or shot["start_seconds"] - prior["end_seconds"] > 180:
                continue
            before = prior.get("fingerprints", [])
            if len({sample["hash"] for sample in before}) < 4:
                continue
            matches, last = [], -1
            for sample in current:
                options = [(int(sample["hash"], 16) ^ int(other["hash"], 16)).bit_count()
                           if j > last else 65 for j, other in enumerate(before)]
                if options and min(options) <= 5:
                    last = options.index(min(options))
                    matches.append((sample, before[last]))
            if (len(matches) >= 4 and len(matches) / max(len(current), 1) >= .65
                    and matches[-1][0]["timestamp_seconds"] - matches[0][0]["timestamp_seconds"] >= 1
                    and len({pair[0]["hash"] for pair in matches}) >= 4):
                candidates.append((prior, matches))
        if len(candidates) == 1:
            prior, matches = candidates[0]
            shot["replay_evidence"] = {"source": "visual_sequence_repeat", "validated": False,
                                       "target_shot_id": prior["id"], "confidence": len(matches) / len(current),
                                       "corroborating_marker": existing or None,
                                       "matched_pts_pairs": [[a["timestamp_seconds"], b["timestamp_seconds"]] for a, b in matches]}
