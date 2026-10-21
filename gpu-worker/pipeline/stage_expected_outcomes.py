"""Routed worker stage for experimental expected outcomes with explicit abstention."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline import backend, model_registry_client, model_router
from pipeline.expected_outcomes.data import DEFINITIONS, MODEL_TYPES, DataError, validate_snapshot
from pipeline.expected_outcomes.extract import extract_snapshot
from pipeline.expected_outcomes.model import load_model, predict


def run(
    clip_id: str,
    job_id: str,
    priority: int,
    *,
    tracklets: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    lineage: dict[str, str] | None = None,
    analytics_safe: bool = False,
    timeline: Any = None,
    attacking_direction: int | None = None,
    down: int | None = None,
    distance_yards: float | None = None,
    capture_regime: str = "unknown",
    snapshots: dict[str, dict[str, Any]] | None = None,
    model_paths: dict[str, tuple[Path, str]] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    variant = model_router.select_model("expected_outcomes", priority)
    predictions, feature_snapshots = {}, {}
    for task in DEFINITIONS:
        try:
            if variant != "expected-outcomes-linear-v1":
                raise DataError("Unsupported routed expected-outcome variant")
            packet = (snapshots or {}).get(task)
            if packet is None:
                packet = extract_snapshot(
                    task,
                    tracklets or [],
                    events or [],
                    lineage or {},
                    analytics_safe=analytics_safe,
                    timeline=timeline,
                    attacking_direction=attacking_direction,
                    down=down,
                    distance_yards=distance_yards,
                    capture_regime=capture_regime,
                )
            snapshot = validate_snapshot(packet)
            feature_snapshots[task] = packet
            path_hash = (model_paths or {}).get(task)
            approved = None
            if path_hash is None:
                resolved = model_registry_client.resolve(MODEL_TYPES[task])
                if (
                    resolved is None
                    or not resolved.validated
                    or not resolved.artifact_sha256
                    or not resolved.approved_evaluation_id
                ):
                    raise DataError("No externally approved artifact-bound model available")
                approved = resolved.publication_approvals.get(MODEL_TYPES[task])
                if (
                    not isinstance(approved, dict)
                    or approved.get("feature_version") != snapshot.feature_version
                    or snapshot.capture_regime not in approved.get("capture_regimes", [])
                ):
                    raise DataError(
                        "Model is not approved for this feature version or film category"
                    )
                path_hash = resolved.local_path, resolved.artifact_sha256
            model = load_model(Path(path_hash[0]), path_hash[1])
            result = predict(model, snapshot)
            if approved is not None:
                result.update(
                    experimental=False,
                    analytics_safe=True,
                    validated=True,
                    model_version_id=resolved.model_version_id,
                    definition_version=approved["definition_version"],
                    provenance={
                        "source": "real",
                        "validated": True,
                        "artifact_sha256": resolved.artifact_sha256,
                        "approved_evaluation_id": resolved.approved_evaluation_id,
                        "definition_version": approved["definition_version"],
                        "quality_checks_passed": True,
                        "evidence": {
                            "heldout_evaluation": resolved.approved_evaluation_id,
                            "model_approval": resolved.model_version_id,
                            "artifact_bound_feature_snapshot": list(snapshot.evidence_hashes),
                            "source_clock": result["observation_clock"],
                            "reviewed_outcome_labels": resolved.approved_evaluation_id,
                        },
                    },
                )
        except (DataError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            result = {
                "task": task,
                "value": None,
                "suppressed": True,
                "reason": str(exc),
                "experimental": True,
                "analytics_safe": False,
                "validated": False,
            }
        predictions[task] = result
        if persist:
            # A failed write must fail the stage so the queue can retry it.
            backend.create_metric(
                clip_id=clip_id,
                metric_name=MODEL_TYPES[task],
                metric_value=result,
                tracklet_id=(result.get("subject") or {}).get("tracklet_id"),
                unit="prob" if DEFINITIONS[task].binary else "yd",
                is_suppressed=result["suppressed"],
                suppression_reason=result.get("reason"),
                experimental_flag=result["experimental"],
                analytics_safe=result["analytics_safe"],
                model_version_id=result.get("model_version_id"),
                job_id=job_id,
            )
    return {
        "clip_id": clip_id,
        "predictions": predictions,
        "feature_snapshots": feature_snapshots,
        "model_routing": {"expected_outcomes": variant},
        "experimental": True,
    }
