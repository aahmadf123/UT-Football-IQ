"""Reproducible NumPy fitting, held-out evaluation and abstaining inference.

Binary tasks reuse the existing pressure logistic head and Platt calibrator.
Yardage uses least-squares with an intercept, then calibration-set bias correction.
The reference prediction is always the empirical TRAIN mean, never a serving fallback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.expected_outcomes.data import (
    DataError,
    Snapshot,
    definitions,
    finite,
    nonempty_string,
    sha256,
)
from pipeline.pressure.classifier import PressureModel
from pipeline.runtime import check_control


def _arrays(rows: list[Snapshot]) -> tuple[np.ndarray, np.ndarray]:
    return np.array([r.vector for r in rows]), np.array([r.target for r in rows])


def _snapshot_hash(row: Snapshot) -> str:
    return hashlib.sha256(
        json.dumps(asdict(row), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def fit(rows: list[Snapshot], task: str) -> dict[str, Any]:
    spec = definitions(task)
    groups = {
        split: [r for r in rows if r.task == task and r.split == split]
        for split in ("train", "calibration", "holdout")
    }
    if any(len(group) < 2 for group in groups.values()):
        raise DataError("All three disjoint reviewed splits are required")
    seen, plays = {}, set()
    for split, group in groups.items():
        if spec.binary and {r.target for r in group} != {0.0, 1.0}:
            raise DataError("Binary fitting and evaluation require both reviewed classes")
        for row in group:
            if row.target is None or not row.annotation_sha256:
                raise DataError("Fitting requires reviewed targets")
            if (
                row.feature_version != spec.version
                or row.population != spec.population
                or len(row.vector) != len(spec.features)
            ):
                raise DataError("Fitting feature contract mismatch")
            for key, value in row.lineage:
                if seen.setdefault((key, value), split) != split:
                    raise DataError("Fitting split lineage leakage")
            play = dict(row.lineage)["canonical_play_id"]
            if play in plays:
                raise DataError("Duplicate canonical play")
            plays.add(play)
    x, y = _arrays(groups["train"])
    cx, cy = _arrays(groups["calibration"])
    check_control()
    if spec.binary:
        fitted = PressureModel().fit(x, y).calibrate(cx, cy)
        parameters = fitted.to_checkpoint()
        # PressureModel's shared head works for both binary tasks; publish actual feature names.
        parameters["feature_names"] = [name for name, _unit in spec.features]
    else:
        mean, scale = x.mean(axis=0), x.std(axis=0)
        scale = np.where(scale < 1e-12, 1.0, scale)
        design = np.column_stack((np.ones(len(x)), (x - mean) / scale))
        coef = np.linalg.lstsq(design, y, rcond=None)[0]
        calibration_raw = np.column_stack((np.ones(len(cx)), (cx - mean) / scale)) @ coef
        parameters = {
            "model": "standardized-least-squares",
            "weight": coef[1:].tolist(),
            "bias": float(coef[0]),
            "feat_mean": mean.tolist(),
            "feat_std": scale.tolist(),
            "calibration_bias": float(np.mean(cy - calibration_raw)),
        }
    return {
        "schema_version": 1,
        "task": task,
        "feature_version": spec.version,
        "feature_definitions": [{"name": n, "unit": u} for n, u in spec.features],
        "eligible_population": spec.population,
        "clock": spec.clock,
        "parameters": parameters,
        "baseline_mean": float(y.mean()),
        "experimental": True,
        "promoted": False,
        "split_lineage": {
            split: [dict(r.lineage) for r in group] for split, group in groups.items()
        },
        "split_snapshot_hashes": {
            split: sorted(_snapshot_hash(r) for r in group) for split, group in groups.items()
        },
        "fitting": {
            "binary": "pressure-logistic-lr0.1-epochs500-l2_0.001+platt",
            "yardage": "numpy-lstsq-rcond_none+calibration-set-mean-residual",
            "holdout_used_for_fitting": False,
        },
    }


def save_model(model: dict[str, Any], path: Path) -> str:
    encoded = json.dumps(model, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded)
    return hashlib.sha256(encoded).hexdigest()


def load_model(path: Path, expected_sha256: str) -> dict[str, Any]:
    expected_sha256 = sha256(expected_sha256)
    encoded = path.read_bytes()
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != expected_sha256:
        raise DataError("Model weights hash mismatch")
    try:
        model = json.loads(encoded)
        spec = definitions(model["task"])
        if (
            type(model["schema_version"]) is not int
            or model["schema_version"] != 1
            or model["feature_version"] != spec.version
            or model["eligible_population"] != spec.population
            or model["clock"] != spec.clock
            or model["feature_definitions"] != [{"name": n, "unit": u} for n, u in spec.features]
        ):
            raise DataError("Incompatible model feature contract")
        params = model["parameters"]
        n = len(spec.features)
        for key in ("weight", "feat_mean", "feat_std"):
            if len(params[key]) != n or any(not np.isfinite(finite(v)) for v in params[key]):
                raise DataError("Malformed model dimensions")
        if any(v <= 0 for v in params["feat_std"]):
            # Logistic training permits constant columns; its standardizer treats zero as one.
            if not spec.binary or any(v < 0 for v in params["feat_std"]):
                raise DataError("Invalid model scales")
        finite(params["bias"])
        finite(model["baseline_mean"])
        if spec.binary:
            platt = params["platt"]
            if params["feature_names"] != [name for name, _unit in spec.features]:
                raise DataError("Binary head feature order mismatch")
            if platt["is_fitted"] is not True:
                raise DataError("Missing fitted probability calibrator")
            finite(platt["a"])
            finite(platt["b"])
        else:
            finite(params["calibration_bias"])
        _check_split_lineage(model)
        for split in ("train", "calibration", "holdout"):
            hashes = model["split_snapshot_hashes"][split]
            if not isinstance(hashes, list) or len(hashes) != len(model["split_lineage"][split]):
                raise DataError("Missing immutable snapshot hashes")
            for digest in hashes:
                sha256(digest)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DataError(f"Invalid model artifact: {exc}") from exc
    return {**model, "weights_sha256": actual}


def _check_split_lineage(model: dict[str, Any]) -> None:
    seen = {}
    for split in ("train", "calibration", "holdout"):
        lineage = model["split_lineage"][split]
        if not isinstance(lineage, list) or not lineage:
            raise DataError("Missing model split lineage")
        for entry in lineage:
            if not isinstance(entry, dict):
                raise DataError("Malformed model lineage entry")
            for key in ("session_id", "recording_id", "canonical_play_id", "media_sha256"):
                nonempty_string(entry.get(key))
            sha256(entry["media_sha256"])
            for key, value in entry.items():
                nonempty_string(value)
                previous = seen.setdefault((key, value), split)
                if previous != split:
                    raise DataError("Model split leakage")


def _values(model: dict[str, Any], x: np.ndarray) -> np.ndarray:
    if definitions(model["task"]).binary:
        head = PressureModel.from_checkpoint(model["parameters"])
        return np.array([head.probability(vector) for vector in x])
    p = model["parameters"]
    return (
        (x - np.array(p["feat_mean"])) / np.array(p["feat_std"]) @ np.array(p["weight"])
        + p["bias"]
        + p["calibration_bias"]
    )


def predict(model: dict[str, Any], snapshot: Snapshot) -> dict[str, Any]:
    spec = definitions(snapshot.task)
    if model["task"] != snapshot.task or model["feature_version"] != snapshot.feature_version:
        raise DataError("Snapshot and model feature contract differ")
    if not model.get("weights_sha256"):
        raise DataError("Inference requires verified persisted model weights")
    value = float(_values(model, np.array([snapshot.vector]))[0])
    if not np.isfinite(value):
        raise DataError("Model produced nonfinite output")
    return {
        "task": snapshot.task,
        "value": value,
        "unit": "prob" if spec.binary else "yd",
        "suppressed": False,
        "experimental": True,
        "analytics_safe": False,
        "weights_sha256": model["weights_sha256"],
        "feature_version": snapshot.feature_version,
        "observation_clock": {"event": spec.clock, "timestamp_seconds": snapshot.timestamp},
        "eligible_population": snapshot.population,
        "capture_regime": snapshot.capture_regime,
        "lineage": dict(snapshot.lineage),
        "subject": dict(snapshot.subject),
        "feature_evidence_sha256": list(snapshot.evidence_hashes),
        "calibration_method": "platt" if spec.binary else "calibration_set_bias_correction",
        "validated": False,
    }


def _metrics(y: np.ndarray, prediction: np.ndarray, binary: bool) -> dict[str, Any]:
    from pipeline.expected_outcomes.diagnostics import outcome_diagnostics
    bins = []
    if binary:
        prediction = np.clip(prediction, 1e-12, 1 - 1e-12)
        assignments = np.minimum((prediction * 10).astype(int), 9)
        for index in range(10):
            lower = index / 10
            mask = assignments == index
            if np.any(mask):
                bins.append(
                    {
                        "lower": float(lower),
                        "upper": float(lower + 0.1),
                        "count": int(mask.sum()),
                        "predicted_mean": float(prediction[mask].mean()),
                        "observed_mean": float(y[mask].mean()),
                    }
                )
        return {
            "brier": float(np.mean((prediction - y) ** 2)),
            **outcome_diagnostics(y.tolist(), prediction.tolist(), binary),
            "log_loss": float(-np.mean(y * np.log(prediction) + (1 - y) * np.log(1 - prediction))),
            "ece": sum(b["count"] * abs(b["predicted_mean"] - b["observed_mean"]) for b in bins)
            / len(y),
            "calibration_bins": bins,
        }
    edges = np.unique(np.quantile(prediction, np.linspace(0, 1, 6)))
    assignments = np.digitize(prediction, edges[1:-1], right=True)
    for group in np.unique(assignments):
        indices = np.flatnonzero(assignments == group)
        bins.append(
            {
                "count": len(indices),
                "predicted_mean": float(prediction[indices].mean()),
                "observed_mean": float(y[indices].mean()),
            }
        )
    residual = prediction - y
    return {
        "mae": float(np.mean(np.abs(residual))),
        **outcome_diagnostics(y.tolist(), prediction.tolist(), binary),
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mean_error": float(residual.mean()),
        "calibration_bins": bins,
        "mean_absolute_calibration_error": sum(
            b["count"] * abs(b["predicted_mean"] - b["observed_mean"]) for b in bins
        )
        / len(y),
    }


def evaluate(model: dict[str, Any], rows: list[Snapshot]) -> dict[str, Any]:
    task = model["task"]
    heldout = [r for r in rows if r.task == task and r.split == "holdout"]
    if not heldout or not model.get("weights_sha256"):
        raise DataError("Evaluation needs a hash-verified model and reviewed holdout")
    for row in heldout:
        if dict(row.lineage) not in model["split_lineage"]["holdout"]:
            raise DataError("Evaluation holdout does not match immutable training manifest")
    if sorted(_snapshot_hash(r) for r in heldout) != model["split_snapshot_hashes"]["holdout"]:
        raise DataError(
            "Evaluation requires the complete immutable holdout features and reviewed targets"
        )
    # Also check the supplied evaluation rows against the saved fitting groups.
    for split in ("train", "calibration"):
        for row in heldout:
            for entry in model["split_lineage"][split]:
                if any(entry.get(k) == v for k, v in row.lineage):
                    raise DataError("Evaluation leakage into a fitting group")
    x, y = _arrays(heldout)
    binary = definitions(task).binary
    metrics = _metrics(y, _values(model, x), binary)
    baseline = _metrics(y, np.full(len(y), model["baseline_mean"]), binary)
    losses = (
        ("brier", "log_loss", "ece")
        if binary
        else ("mae", "rmse", "mean_absolute_calibration_error")
    )
    return {
        "task": task,
        "split": "holdout",
        "feature_version": model["feature_version"],
        "weights_sha256": model["weights_sha256"],
        "eligible_population": model["eligible_population"],
        "count": len(y),
        "group_counts": {
            key: len({dict(r.lineage)[key] for r in heldout if dict(r.lineage).get(key)})
            for key in ("session_id", "game_id", "canonical_play_id")
        },
        "metrics": metrics,
        "baseline": {
            "definition": "empirical_training_population_mean",
            "training_mean": model["baseline_mean"],
            "metrics": baseline,
        },
        "beats_baseline": all(metrics[k] < baseline[k] for k in losses),
        "experimental": True,
        "promoted": False,
        "release_gate_passed": False,
        "human_promotion_required": True,
    }
