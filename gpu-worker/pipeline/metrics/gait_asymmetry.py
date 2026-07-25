"""Left/right stride (gait) asymmetry from pose keypoint sequences (Issue #149).

Pure functions over the keypoint sequences built by ``stage_pose`` — no DB,
no video, no tracklet attribution. Attribution and identity gating are the
caller's job so uncertain CV output never becomes a player label here.

The primary output is ``asymmetry_index``: the ratio of the longer-stride
side's mean displacement to the shorter side's (1.0 = perfectly symmetric).
Issue #149's alert threshold ``asymmetry > 1.3`` reads directly against it.
The legacy normalized-difference ``asymmetry_score`` (|L-R| / mean, risk
threshold 0.15) is kept alongside for backward compatibility with the
``pose_stride_symmetry`` metric.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from pipeline.pose_estimator import kp

# Minimum keypoint confidence for an ankle sample to count toward strides.
DEFAULT_KP_CONFIDENCE_FLOOR = 0.3
# Minimum keypoint-sequence entries before asymmetry is meaningful.
DEFAULT_MIN_SAMPLES = 4


def compute_gait_asymmetry(
    kp_seq: list[dict[str, Any]],
    *,
    kp_confidence_floor: float = DEFAULT_KP_CONFIDENCE_FLOOR,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any] | None:
    """Compute left/right stride asymmetry for one keypoint sequence.

    ``kp_seq`` entries are ``{"frame_number": int, "keypoints": [kp, ...]}``
    as produced by ``stage_pose._build_keypoint_sequence``. Returns ``None``
    when there is not enough confident ankle data to say anything.
    """
    if len(kp_seq) < min_samples:
        return None

    left_strides: list[float] = []
    right_strides: list[float] = []
    confs: list[float] = []
    prev_la: tuple[float, float] | None = None
    prev_ra: tuple[float, float] | None = None

    for entry in kp_seq:
        la = kp(entry["keypoints"], "left_ankle")
        ra = kp(entry["keypoints"], "right_ankle")
        pair_conf = min(la["confidence"], ra["confidence"])
        if pair_conf < kp_confidence_floor:
            continue
        confs.append(pair_conf)

        if prev_la is not None:
            left_strides.append(math.hypot(la["x"] - prev_la[0], la["y"] - prev_la[1]))
        if prev_ra is not None:
            right_strides.append(math.hypot(ra["x"] - prev_ra[0], ra["y"] - prev_ra[1]))

        prev_la = (la["x"], la["y"])
        prev_ra = (ra["x"], ra["y"])

    if not left_strides or not right_strides:
        return None

    mean_left = statistics.mean(left_strides)
    mean_right = statistics.mean(right_strides)
    total = mean_left + mean_right
    if total < 1e-6:
        return None

    ratio = mean_left / mean_right if mean_right > 0 else 1.0
    asymmetry_score = abs(mean_left - mean_right) / (total / 2.0)
    asymmetry_index = max(mean_left, mean_right) / max(min(mean_left, mean_right), 1e-6)

    return {
        "left_stride_mean": round(mean_left, 3),
        "right_stride_mean": round(mean_right, 3),
        "left_right_ratio": round(ratio, 3),
        "asymmetry_score": round(asymmetry_score, 4),
        "asymmetry_index": round(asymmetry_index, 3),
        "sample_count": min(len(left_strides), len(right_strides)),
        "confidence": round(float(statistics.mean(confs)), 3),
    }
