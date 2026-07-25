"""Body-orientation proxy from RTMPose-style keypoints.

This estimates head/body direction from pose geometry only. It is not eye
tracking and never requires face recognition.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

MIN_KEYPOINT_CONFIDENCE = 0.35


def compute_head_yaw(keypoints: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute head yaw in degrees using nose, ears, and shoulders.

    Formula mirrors issue #143:
    ``atan2(nose_x - ear_mid_x, nose_y - ear_mid_y)``.
    """

    nose = _visible(keypoints, "nose")
    left_ear = _visible(keypoints, "left_ear")
    right_ear = _visible(keypoints, "right_ear")
    left_shoulder = _visible(keypoints, "left_shoulder")
    right_shoulder = _visible(keypoints, "right_shoulder")
    required = [nose, left_ear, right_ear, left_shoulder, right_shoulder]
    if any(kp is None for kp in required):
        return None

    assert nose is not None
    assert left_ear is not None
    assert right_ear is not None
    assert left_shoulder is not None
    assert right_shoulder is not None

    ear_mid_x = (float(left_ear["x"]) + float(right_ear["x"])) / 2.0
    ear_mid_y = (float(left_ear["y"]) + float(right_ear["y"])) / 2.0
    dx = float(nose["x"]) - ear_mid_x
    dy = float(nose["y"]) - ear_mid_y
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None

    yaw = _normalize_degrees(math.degrees(math.atan2(dx, dy)))
    confidences = [
        float(kp.get("confidence", 0.0))
        for kp in (nose, left_ear, right_ear, left_shoulder, right_shoulder)
    ]
    shoulder_width = math.hypot(
        float(right_shoulder["x"]) - float(left_shoulder["x"]),
        float(right_shoulder["y"]) - float(left_shoulder["y"]),
    )
    confidence = max(0.0, min(1.0, (sum(confidences) / len(confidences)) * min(1.0, shoulder_width / 40.0)))
    orientation_class = classify_body_orientation_proxy(yaw)
    return {
        "head_yaw_degrees": round(yaw, 3),
        "orientation_class": orientation_class,
        "confidence": round(confidence, 3),
    }


def summarize_body_orientation(
    kp_seq: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Aggregate frame-level yaw results into a tracklet-level proxy summary."""

    results = [
        result
        for entry in kp_seq
        if (result := compute_head_yaw(entry.get("keypoints", []))) is not None
    ]
    if not results:
        return None

    sin_sum = sum(math.sin(math.radians(float(r["head_yaw_degrees"]))) for r in results)
    cos_sum = sum(math.cos(math.radians(float(r["head_yaw_degrees"]))) for r in results)
    avg_yaw = _normalize_degrees(math.degrees(math.atan2(sin_sum, cos_sum)))
    avg_conf = sum(float(r["confidence"]) for r in results) / len(results)
    class_counts = Counter(str(r["orientation_class"]) for r in results)
    orientation_class = class_counts.most_common(1)[0][0]
    reason_codes: list[str] = []
    if avg_conf < 0.70:
        reason_codes.append("low_pose_confidence")
    return {
        "head_yaw_deg": round(avg_yaw, 3),
        "orientation_class": orientation_class,
        "confidence": round(avg_conf, 3),
        "frame_count": len(results),
        "review_state": "needs_coach_review",
        "reason_codes": reason_codes,
    }


def classify_body_orientation_proxy(yaw_degrees: float) -> str:
    """Classify pose-derived orientation without claiming true gaze."""

    if yaw_degrees <= -35.0:
        return "body_inside"
    if yaw_degrees >= 35.0:
        return "body_on_receiver"
    return "body_backfield"


def _visible(keypoints: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in keypoints:
        if item.get("name") != name:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
            float(item["x"])
            float(item["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if confidence >= MIN_KEYPOINT_CONFIDENCE:
            return item
    return None


def _normalize_degrees(degrees: float) -> float:
    while degrees <= -180.0:
        degrees += 360.0
    while degrees > 180.0:
        degrees -= 360.0
    return degrees

