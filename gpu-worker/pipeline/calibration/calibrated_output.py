"""``CalibratedOutput`` envelope + multi-class temperature scaling (Issue #146).

Every Phase-CV classifier (coverage GNN #139, pressure predictor #140) returns a
:class:`CalibratedOutput`, which serialises to the Issue #146 contract::

    {"value": ..., "confidence": 0.82, "calibration_method": "platt"}

plus the supporting detail a coach UI needs (full class probabilities,
normalised entropy as the uncertainty signal, and an ``is_calibrated`` flag).

The honesty rule from the rest of the pipeline holds: an output whose
probabilities have **not** been run through a fitted calibrator reports
``calibration_method="uncalibrated"`` and ``is_calibrated=False`` so a raw
softmax is never presented as a calibrated confidence. Such an output is
flagged ``experimental=True`` — it can still be shown, but clearly badged.

:class:`TemperatureScaler` is the multi-class analogue of
:class:`~pipeline.play_prediction.calibration.PlattScaler`: a single temperature
``T`` fitted by minimising NLL, the standard post-hoc calibrator for a softmax
classifier. Pure NumPy (``scipy``/``sklearn`` are deliberately not worker deps).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pipeline.play_prediction.calibration import reliability_curve

_EPS = 1e-12

UNCALIBRATED = "uncalibrated"


def _softmax(z: np.ndarray, axis: int = -1) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64)
    z = z - np.max(z, axis=axis, keepdims=True)
    e = np.exp(z)
    return e / np.clip(np.sum(e, axis=axis, keepdims=True), _EPS, None)


def normalized_entropy(probs: np.ndarray | list[float]) -> float:
    """Shannon entropy of a categorical distribution, scaled to ``[0, 1]``.

    1.0 == uniform (maximally uncertain), 0.0 == one-hot. This is the
    uncertainty signal surfaced alongside every prediction (Issue #146); for a
    binary head it coincides with the existing ``binary_entropy``.
    """
    p = np.clip(np.asarray(probs, dtype=np.float64).ravel(), _EPS, 1.0)
    k = p.size
    if k <= 1:
        return 0.0
    h = float(-np.sum(p * np.log(p)))
    return float(h / np.log(k))


# ── Multi-class temperature scaling ───────────────────────────────────────────


@dataclass
class TemperatureScaler:
    """Single-temperature softmax calibration ``softmax(logits / T)``.

    Unfitted (``T == 1``) it is the identity, so it is always safe to apply and
    never fabricates calibration.
    """

    temperature: float = 1.0
    is_fitted: bool = False

    def fit(
        self,
        logits: np.ndarray | list[list[float]],
        labels: np.ndarray | list[int],
        *,
        lr: float = 0.05,
        max_iter: int = 200,
        tol: float = 1e-6,
    ) -> TemperatureScaler:
        """Fit ``T`` by minimising NLL on held-out ``(logits, label)`` pairs."""
        z = np.asarray(logits, dtype=np.float64)
        y = np.asarray(labels, dtype=np.int64).ravel()
        if z.ndim != 2 or z.shape[0] != y.size or z.shape[0] == 0:
            raise ValueError("logits must be (N, K) aligned with labels (N,)")

        log_t = 0.0  # optimise in log-space so T stays positive
        n = z.shape[0]
        idx = np.arange(n)
        for _ in range(max_iter):
            t = float(np.exp(log_t))
            probs = _softmax(z / t, axis=1)
            # d(NLL)/d(log_t): chain rule through T = exp(log_t).
            scaled = z / t
            expected = np.sum(probs * scaled, axis=1)
            grad = float(np.mean(scaled[idx, y] - expected))  # d NLL / d log_t
            step = lr * grad
            log_t -= step
            if abs(step) < tol:
                break

        self.temperature = float(np.exp(log_t))
        self.is_fitted = True
        return self

    def transform_logits(self, logits: np.ndarray | list[float]) -> np.ndarray:
        z = np.asarray(logits, dtype=np.float64)
        return _softmax(z / max(self.temperature, _EPS), axis=-1)

    def transform_probs(self, probs: np.ndarray | list[float]) -> np.ndarray:
        """Re-temper an already-softmaxed distribution via its log-probs."""
        p = np.clip(np.asarray(probs, dtype=np.float64), _EPS, 1.0)
        return _softmax(np.log(p) / max(self.temperature, _EPS), axis=-1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "is_fitted": self.is_fitted,
            "kind": "temperature",
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TemperatureScaler:
        return cls(
            temperature=float(d.get("temperature", 1.0)),
            is_fitted=bool(d.get("is_fitted", False)),
        )


def multiclass_ece(
    probs: np.ndarray | list[list[float]],
    labels: np.ndarray | list[int],
    n_bins: int = 10,
) -> float:
    """Top-label expected calibration error for a multi-class classifier.

    Bins predictions by the max-probability ("confidence") and compares each
    bin's confidence to its empirical accuracy — the standard ECE for the
    Issue #146 ``ECE < 0.05`` production gate (reuses the binary reliability
    binning so the math is shared, not re-implemented).
    """
    p = np.asarray(probs, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int64).ravel()
    if p.ndim != 2 or p.shape[0] != y.size or p.shape[0] == 0:
        return float("nan")
    confidences = np.max(p, axis=1)
    predictions = np.argmax(p, axis=1)
    correct = (predictions == y).astype(np.float64)
    # reliability_curve expects (prob, label) and returns |acc - conf| per bin.
    curve = reliability_curve(confidences, correct, n_bins=n_bins)
    n = float(p.shape[0])
    return float(
        sum(
            (b["count"] / n) * abs(b["empirical_accuracy"] - b["mean_predicted"])
            for b in curve
        )
    )


# ── The output envelope ───────────────────────────────────────────────────────


@dataclass(slots=True)
class CalibratedOutput:
    """A classification result with a calibrated confidence (Issue #146)."""

    value: str | float
    confidence: float
    calibration_method: str = UNCALIBRATED
    is_calibrated: bool = False
    probabilities: dict[str, float] | None = None
    entropy: float = 0.0
    labels: tuple[str, ...] | None = None
    ece: float | None = None  # ECE measured at calibration-fit time, if known
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def experimental(self) -> bool:
        """Uncalibrated outputs are experimental until a calibrator is fitted."""
        return not self.is_calibrated

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the Issue #146 response contract (+ supporting detail)."""
        out: dict[str, Any] = {
            "value": self.value,
            "confidence": round(float(self.confidence), 4),
            "calibration_method": self.calibration_method,
            "is_calibrated": self.is_calibrated,
            "experimental": self.experimental,
            "uncertainty": round(float(self.entropy), 4),
        }
        if self.probabilities is not None:
            out["probabilities"] = {
                k: round(float(v), 4) for k, v in self.probabilities.items()
            }
        if self.ece is not None:
            out["ece"] = round(float(self.ece), 4)
        if self.detail:
            out["detail"] = self.detail
        return out

    # ── Constructors ──────────────────────────────────────────────────────────

    @classmethod
    def from_multiclass(
        cls,
        probabilities: dict[str, float],
        *,
        calibration_method: str = UNCALIBRATED,
        is_calibrated: bool = False,
        ece: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CalibratedOutput:
        """Build from a ``{label: probability}`` distribution."""
        if not probabilities:
            raise ValueError("probabilities must be non-empty")
        labels = tuple(probabilities.keys())
        vals = np.array([probabilities[k] for k in labels], dtype=np.float64)
        total = float(vals.sum())
        if total <= 0:
            raise ValueError("probabilities must sum to a positive value")
        vals = vals / total
        norm = {k: float(v) for k, v in zip(labels, vals, strict=True)}
        top = int(np.argmax(vals))
        return cls(
            value=labels[top],
            confidence=float(vals[top]),
            calibration_method=calibration_method,
            is_calibrated=is_calibrated,
            probabilities=norm,
            entropy=normalized_entropy(vals),
            labels=labels,
            ece=ece,
            detail=detail or {},
        )

    @classmethod
    def from_binary(
        cls,
        p_positive: float,
        *,
        positive_label: str = "positive",
        negative_label: str = "negative",
        calibration_method: str = UNCALIBRATED,
        is_calibrated: bool = False,
        ece: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CalibratedOutput:
        """Build from a single ``P(positive)`` (the pressure-predictor case)."""
        p = float(min(max(p_positive, 0.0), 1.0))
        return cls.from_multiclass(
            {positive_label: p, negative_label: 1.0 - p},
            calibration_method=calibration_method,
            is_calibrated=is_calibrated,
            ece=ece,
            detail=detail,
        )
