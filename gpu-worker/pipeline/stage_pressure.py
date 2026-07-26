"""Stage — pre-snap pressure prediction (Issue #140).

Runs *after* ``stage_oline`` over the same calibrated, tracked, snap-anchored
play state. Extracts the snap-frame pressure features (OL gaps, DL gap
alignment, LB depth, blitz indicator, down-and-distance), predicts
``P(QB pressured ≤ 2.5 s)`` with calibrated uncertainty, and persists it as a
``pressure_prob`` metric.

Guardrails honoured:

* **No clean-coordinate assumption** — when calibration is not analytics-safe
  the stage suppresses (it never computes pressure on uncalibrated coordinates).
* **Experimental until validated** — the metric is always written
  ``experimental_flag=True`` / ``analytics_safe=False`` (the prediction is a
  prior, not a Toledo-validated number), exactly like the frontier metrics.
* **Not routed** — deterministic over routed-stage outputs with an optional
  offline-trained checkpoint that falls back; registers no ``model_router``
  stage (see ``docs/coverage-pressure-features.md``).
"""

from __future__ import annotations

from typing import Any

import structlog

from pipeline import backend
from pipeline.pressure.classifier import (
    POSITIVE_LABEL,
    PressureClassifier,
)
from pipeline.pressure.feature_extractor import extract_pressure_features
from pipeline.spatial.feature_schema import estimate_los_x

log = structlog.get_logger(__name__)

PRESSURE_METRIC = "pressure_prob"


def run(
    clip_id: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    job_id: str,
    *,
    offense_direction: int | None = None,
    down: int | None = None,
    distance_yards: float | None = None,
    classifier: PressureClassifier | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Predict pre-snap pressure for one clip and persist it (experimental)."""
    log.info("stage_pressure_start", clip_id=clip_id)
    classifier = classifier or PressureClassifier.from_env()

    snap_event = next((e for e in events if e.get("event_type") == "snap"), None)
    snap_frame = snap_event.get("frame_number", 0) if snap_event else 0

    # No clean-coordinate assumption: suppress when calibration isn't safe.
    if not analytics_safe:
        return _suppress(
            clip_id, job_id, "calibration_not_analytics_safe", persist=persist
        )

    # Every feature here is signed by the attacking direction -- the offence /
    # defence split itself is "which side of the ball", and the DL gap labels
    # follow from it. With the direction unresolved the two teams change places,
    # so this is not a degraded number but an inverted one.
    if offense_direction is None:
        return _suppress(
            clip_id, job_id, "field_orientation_unresolved", persist=persist
        )

    los_x = estimate_los_x(tracklets, snap_frame)
    features = extract_pressure_features(
        tracklets,
        snap_frame,
        los_x,
        calibration_confirmed=True,
        offense_direction=offense_direction,
        down=down,
        distance_yards=distance_yards,
    )

    # Too little of the front recovered to say anything → suppress, don't guess.
    if features.ol_count == 0 and features.dl_count == 0:
        return _suppress(clip_id, job_id, "no_front_recovered", persist=persist)

    output = classifier.predict(features)
    # Always report P(pressure) — never the argmax class probability, which
    # could be P(no_pressure) on a low-pressure look.
    pressure_prob = output.probabilities.get(POSITIVE_LABEL) if output.probabilities else None
    metric_value: dict[str, Any] = {
        **output.to_dict(),
        "pressure_prob": pressure_prob,
        "features": features.to_dict(),
        "snap_frame": snap_frame,
        "source": "tracking",
    }

    metric_id: str | None = None
    if persist:
        try:
            resp = backend.create_metric(
                clip_id=clip_id,
                metric_name=PRESSURE_METRIC,
                metric_value=metric_value,
                unit="prob",
                experimental_flag=True,  # never trusted until #140 validation
                analytics_safe=False,
                confidence=output.confidence,
                job_id=job_id,
            )
            metric_id = resp["id"]
        except Exception as exc:  # offline / backend-down → return without persisting
            log.warning("pressure_metric_write_failed", clip_id=clip_id, error=str(exc))

    log.info(
        "stage_pressure_done",
        clip_id=clip_id,
        pressure_value=output.value,
        confidence=round(output.confidence, 3),
        calibrated=output.is_calibrated,
        model=metric_value.get("detail", {}).get("model"),
    )
    return {
        "clip_id": clip_id,
        "pressure": metric_value,
        "metric_id": metric_id,
        "suppressed": False,
    }


def _suppress(
    clip_id: str, job_id: str, reason: str, *, persist: bool
) -> dict[str, Any]:
    metric_value = {
        "suppressed": True,
        "reason": reason,
        "source": "tracking",
        "experimental": True,
    }
    metric_id: str | None = None
    if persist:
        try:
            resp = backend.create_metric(
                clip_id=clip_id,
                metric_name=PRESSURE_METRIC,
                metric_value=metric_value,
                unit="prob",
                is_suppressed=True,
                suppression_reason=reason,
                experimental_flag=True,
                analytics_safe=False,
                job_id=job_id,
            )
            metric_id = resp["id"]
        except Exception as exc:
            log.warning("pressure_metric_write_failed", clip_id=clip_id, error=str(exc))
    log.info("stage_pressure_suppressed", clip_id=clip_id, reason=reason)
    return {
        "clip_id": clip_id,
        "pressure": metric_value,
        "metric_id": metric_id,
        "suppressed": True,
    }
