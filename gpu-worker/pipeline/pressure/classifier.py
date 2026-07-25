"""Pre-snap pressure classifier (Issue #140).

``P(QB pressured within 2.5 s)`` from the snap-frame
:class:`~pipeline.pressure.feature_extractor.PressureFeatures`. Two paths:

* **Deterministic baseline** — a hand-tuned logistic over football-sensible
  terms (extra rushers vs blockers, blitzers, blown-open gaps, A-gap / edge
  pressure, LB depth, long-yardage tee-off). Always available, always flagged
  ``is_calibrated=False`` / experimental: it is a sensible prior, not a
  validated model.

* **Offline-trained model** — a logistic-regression head + Platt calibrator
  fitted by ``fit``/``calibrate`` and loaded at runtime from the path in
  ``PRESSURE_MODEL`` (no hard-coded path). When its Platt scaler is fitted the
  output is reported calibrated (``calibration_method="platt"``).

Either way the result is a :class:`~pipeline.calibration.CalibratedOutput`, so a
raw score is never surfaced as a calibrated probability (Issue #146). Per #164,
a model trained on Big Data Bowl data stays offline/experimental until Toledo
validation — its provenance rides in the checkpoint and is surfaced in
``detail``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from pipeline.calibration.calibrated_output import UNCALIBRATED, CalibratedOutput
from pipeline.play_prediction.calibration import PlattScaler, sigmoid
from pipeline.pressure.feature_extractor import PRESSURE_FEATURE_NAMES, PressureFeatures

log = structlog.get_logger(__name__)

PRESSURE_MODEL_ENV = "PRESSURE_MODEL"
PRESSURE_HORIZON_SECONDS = 2.5
POSITIVE_LABEL = "pressure"
NEGATIVE_LABEL = "no_pressure"

# Deterministic-baseline coefficients, centred so a standard 4-man rush vs a
# 5-man protection on 2nd-and-7 sits near a league-ish ~0.40 pressure prior.
# Documented, not learned — the output is always flagged experimental.
_BASE_BIAS = -0.4
_W_EXTRA_RUSHERS = 0.9  # per rusher beyond the standard 4-vs-5
_W_BLITZ = 0.3
_W_WIDE_GAP = 0.2  # per yard of split beyond ~1.2 yd
_W_A_GAP = 0.4
_W_EDGE = 0.3
_W_BOX = 0.1  # per box defender beyond 6
_W_LB_DEPTH = 0.15  # deeper LBs reduce pressure
_W_LONG_YARDAGE = 0.05  # per yard beyond 7 to go
_REF_LB_DEPTH = 4.5
_REF_GAP = 1.2
_REF_BOX = 6.0
_REF_DISTANCE = 7.0


@dataclass
class PressureModel:
    """Offline-trained logistic head + Platt calibrator over the feature vector."""

    weight: np.ndarray | None = None  # (F,)
    bias: float = 0.0
    feat_mean: np.ndarray | None = None
    feat_std: np.ndarray | None = None
    platt: PlattScaler | None = None
    provenance: dict[str, Any] | None = None

    @property
    def is_loaded(self) -> bool:
        return self.weight is not None

    @property
    def is_calibrated(self) -> bool:
        return self.platt is not None and self.platt.is_fitted

    def _standardize(self, x: np.ndarray) -> np.ndarray:
        if self.feat_mean is None or self.feat_std is None:
            return x
        std = np.where(self.feat_std < 1e-12, 1.0, self.feat_std)
        return (x - self.feat_mean) / std

    def score(self, x: np.ndarray) -> float:
        """Raw decision logit for a single feature vector."""
        if self.weight is None:
            raise RuntimeError("PressureModel has no weights")
        return float(self._standardize(x) @ self.weight + self.bias)

    def probability(self, x: np.ndarray) -> float:
        logit = self.score(x)
        if self.platt is not None and self.platt.is_fitted:
            return float(self.platt.transform_score(logit))
        return float(sigmoid(logit))

    def fit(
        self,
        features: np.ndarray,
        labels: np.ndarray,
        *,
        lr: float = 0.1,
        epochs: int = 500,
        l2: float = 1e-3,
    ) -> PressureModel:
        x = np.asarray(features, dtype=np.float64)
        y = np.asarray(labels, dtype=np.float64).ravel()
        if x.ndim != 2 or x.shape[0] != y.size or x.shape[0] == 0:
            raise ValueError("features must be (N, F) aligned with labels (N,)")
        self.feat_mean = x.mean(axis=0)
        self.feat_std = x.std(axis=0)
        xs = self._standardize(x)
        n, f = xs.shape
        w = np.zeros(f)
        b = 0.0
        for _ in range(epochs):
            p = sigmoid(xs @ w + b)
            grad_w = xs.T @ (p - y) / n + l2 * w
            grad_b = float(np.mean(p - y))
            w -= lr * grad_w
            b -= lr * grad_b
        self.weight = w
        self.bias = float(b)
        return self

    def calibrate(self, features: np.ndarray, labels: np.ndarray) -> PressureModel:
        if self.weight is None:
            raise RuntimeError("fit before calibrating")
        x = self._standardize(np.asarray(features, dtype=np.float64))
        scores = x @ self.weight + self.bias
        self.platt = PlattScaler().fit(scores, np.asarray(labels, dtype=np.int64))
        return self

    def to_checkpoint(self) -> dict[str, Any]:
        if self.weight is None:
            raise RuntimeError("cannot serialise an unloaded PressureModel")
        return {
            "model": "pressure-logistic",
            "feature_names": list(PRESSURE_FEATURE_NAMES),
            "weight": self.weight.tolist(),
            "bias": self.bias,
            "feat_mean": None if self.feat_mean is None else self.feat_mean.tolist(),
            "feat_std": None if self.feat_std is None else self.feat_std.tolist(),
            "platt": None if self.platt is None else self.platt.to_dict(),
            "provenance": self.provenance or {},
        }

    @classmethod
    def from_checkpoint(cls, d: dict[str, Any]) -> PressureModel:
        platt = d.get("platt")
        return cls(
            weight=np.asarray(d["weight"], dtype=np.float64),
            bias=float(d.get("bias", 0.0)),
            feat_mean=None if d.get("feat_mean") is None else np.asarray(d["feat_mean"], dtype=np.float64),
            feat_std=None if d.get("feat_std") is None else np.asarray(d["feat_std"], dtype=np.float64),
            platt=PlattScaler.from_dict(platt) if platt else None,
            provenance=d.get("provenance", {}),
        )

    @classmethod
    def load_from_env(cls, env_var: str = PRESSURE_MODEL_ENV) -> PressureModel | None:
        path_str = os.environ.get(env_var)
        if not path_str:
            return None
        path = Path(path_str)
        if not path.is_file():
            log.warning("pressure_model_missing", path=path_str)
            return None
        try:
            return cls.from_checkpoint(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("pressure_model_unreadable", path=path_str, error=str(exc))
            return None


def _baseline_probability(feats: PressureFeatures) -> float:
    """Deterministic, documented pressure prior (Issue #140, experimental)."""
    extra_rushers = feats.rusher_blocker_diff + 1.0  # 4-vs-5 ⇒ diff -1 ⇒ 0 extra
    distance_yards = feats.distance_yards if feats.distance_yards is not None else _REF_DISTANCE
    logit = (
        _BASE_BIAS
        + _W_EXTRA_RUSHERS * extra_rushers
        + _W_BLITZ * feats.blitzers
        + _W_WIDE_GAP * max(feats.max_gap_width - _REF_GAP, 0.0)
        + _W_A_GAP * feats.dl_gap_counts.get("a", 0)
        + _W_EDGE * feats.dl_gap_counts.get("edge", 0)
        + _W_BOX * max(feats.box_count - _REF_BOX, 0.0)
        - _W_LB_DEPTH * (feats.mean_lb_depth - _REF_LB_DEPTH)
        + _W_LONG_YARDAGE * max(distance_yards - _REF_DISTANCE, 0.0)
    )
    return float(sigmoid(logit))


@dataclass
class PressureClassifier:
    """Predict ``P(pressure ≤ 2.5 s)`` with calibrated uncertainty (Issue #140)."""

    model: PressureModel | None = None

    @classmethod
    def from_env(cls) -> PressureClassifier:
        return cls(model=PressureModel.load_from_env())

    def predict(self, feats: PressureFeatures) -> CalibratedOutput:
        detail: dict[str, Any] = {
            "horizon_seconds": PRESSURE_HORIZON_SECONDS,
            "feature_confidence": round(feats.feature_confidence, 3),
            "blitz_indicator": feats.blitz_indicator,
            "rusher_blocker_diff": feats.rusher_blocker_diff,
            "dl_gap_counts": dict(feats.dl_gap_counts),
        }

        if self.model is not None and self.model.is_loaded:
            p = self.model.probability(feats.to_vector())
            calibrated = self.model.is_calibrated
            method = "platt" if calibrated else UNCALIBRATED
            detail["model"] = "pressure-logistic"
            detail["provenance"] = self.model.provenance or {}
        else:
            p = _baseline_probability(feats)
            calibrated = False
            method = UNCALIBRATED
            detail["model"] = "pressure-baseline-heuristic"

        return CalibratedOutput.from_binary(
            p,
            positive_label=POSITIVE_LABEL,
            negative_label=NEGATIVE_LABEL,
            calibration_method=method,
            is_calibrated=calibrated,
            detail=detail,
        )
