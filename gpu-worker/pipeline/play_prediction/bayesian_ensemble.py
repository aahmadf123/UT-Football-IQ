"""Naive-Bayes log-odds run/pass ensemble + Platt calibration (Issue #136).

Combination (issue math):

    logit P(pass | s_1..s_6) = logit P0 + Σ_i log Λ_i
    Λ_i = P(s_i | pass) / P(s_i | run)
    P(pass) = sigmoid(logit P0 + Σ_i log Λ_i)

``logit P0`` is the situational base log-odds — supplied by the per-opponent
Dirichlet store (Issue #136) when available, otherwise the documented D1 CFB
neutral base rate (~55% pass; see ``docs/pre-snap-prediction.md`` for
provenance). Each signal's log-likelihood-ratio is **weighted by that signal's
confidence**, so a low-confidence / fallback signal contributes proportionally
less rather than corrupting the combination — this is the graceful-degradation
contract from Issue #135.

Calibration (:class:`~pipeline.play_prediction.calibration.PlattScaler`) maps
the raw combined probability to a calibrated one; an unfitted scaler is the
identity, and ``calibrated`` is flagged ``is_calibrated=False`` so a raw
probability is never presented as calibrated.

The optional :class:`MixtureOfExperts` refines the binary call into the
multi-class taxonomy (run-inside / run-outside / play-action / screen). It is
only consulted when a fitted instance is supplied — an unfitted MoE returns
``None`` rather than a fabricated distribution (no mock predictions).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from pipeline.play_prediction.calibration import (
    PlattScaler,
    binary_entropy,
    sigmoid,
)

# Documented D1 CFB neutral-situation pass rate (Issue #136 references). Used
# only when no situational opponent prior is available — never an opponent
# tendency.
D1_NEUTRAL_PASS_RATE = 0.55

PASS = "pass"
RUN = "run"

MULTICLASS_LABELS = ("run_inside", "run_outside", "play_action", "screen")

# Cap a single signal's |log Λ| so one mislabelled categorical value cannot
# dominate the combination (keeps the ensemble well-behaved on sparse data).
_MAX_ABS_LOG_LR = 4.0


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


@dataclass
class LikelihoodRatioModel:
    """Per-signal categorical log-likelihood-ratios with Laplace smoothing.

    Stored as ``log_lr[signal][value] = log( P(value|pass) / P(value|run) )``.
    Unfitted, every lookup returns 0.0 (no evidence), so the ensemble cleanly
    falls back to the base rate.
    """

    log_lr: dict[str, dict[str, float]] = field(default_factory=dict)
    is_fitted: bool = False
    laplace_alpha: float = 1.0

    def fit(self, samples: list[tuple[dict[str, Any], str]]) -> LikelihoodRatioModel:
        """Fit from ``(signal_values, label)`` pairs (label in {'pass','run'})."""
        pass_counts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        run_counts: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        n_pass = 0
        n_run = 0
        signal_values: dict[str, set[str]] = defaultdict(set)

        for signals, label in samples:
            counts = pass_counts if label == PASS else run_counts
            if label == PASS:
                n_pass += 1
            elif label == RUN:
                n_run += 1
            else:
                raise ValueError(f"label must be '{PASS}'/'{RUN}', got {label!r}")
            for name, value in signals.items():
                key = _categorize(value)
                counts[name][key] += 1.0
                signal_values[name].add(key)

        out: dict[str, dict[str, float]] = {}
        alpha = self.laplace_alpha
        for name, values in signal_values.items():
            k = len(values)
            out[name] = {}
            for value in values:
                p_pass = (pass_counts[name].get(value, 0.0) + alpha) / (n_pass + alpha * k)
                p_run = (run_counts[name].get(value, 0.0) + alpha) / (n_run + alpha * k)
                lr = math.log(p_pass / p_run)
                out[name][value] = float(max(-_MAX_ABS_LOG_LR, min(_MAX_ABS_LOG_LR, lr)))

        self.log_lr = out
        self.is_fitted = n_pass > 0 and n_run > 0
        return self

    def lookup(self, signal: str, value: Any) -> float:
        table = self.log_lr.get(signal)
        if not table:
            return 0.0
        return float(table.get(_categorize(value), 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {"log_lr": self.log_lr, "is_fitted": self.is_fitted, "laplace_alpha": self.laplace_alpha}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LikelihoodRatioModel:
        return cls(
            log_lr={k: dict(v) for k, v in d.get("log_lr", {}).items()},
            is_fitted=bool(d.get("is_fitted", False)),
            laplace_alpha=float(d.get("laplace_alpha", 1.0)),
        )


def _categorize(value: Any) -> str:
    """Coerce a signal value to a stable categorical key."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        # Round continuous values into 0.1 buckets so the categorical table
        # stays finite. Callers that want finer control should pre-bucket.
        return f"{round(value, 1):.1f}"
    return str(value)


@dataclass
class PredictionResult:
    """Calibrated run/pass prediction with uncertainty (Issues #136 / #146)."""

    p_pass_raw: float
    calibrated_prob: float
    logit_score: float
    predicted_class: str
    confidence: float
    uncertainty: float
    is_calibrated: bool
    base_log_odds: float
    signal_contributions: dict[str, float]
    opponent_prior_applied: bool
    multiclass: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class BayesianEnsemble:
    """Naive-Bayes log-odds ensemble with optional Platt calibration + MoE."""

    lr_model: LikelihoodRatioModel = field(default_factory=LikelihoodRatioModel)
    platt: PlattScaler = field(default_factory=PlattScaler)
    base_pass_rate: float = D1_NEUTRAL_PASS_RATE
    moe: MixtureOfExperts | None = None

    def predict(
        self,
        signals: dict[str, Any],
        *,
        base_log_odds: float | None = None,
        signal_confidences: dict[str, float] | None = None,
        feature_vector: np.ndarray | None = None,
    ) -> PredictionResult:
        """Combine signals into a calibrated P(pass) + uncertainty.

        Args:
            signals: ``{signal_name: value}`` (the 6 pre-snap signals).
            base_log_odds: situational ``logit(P0)`` from the opponent prior.
                ``None`` → the documented D1 neutral base rate.
            signal_confidences: per-signal ``[0,1]`` weights; missing → 1.0.
            feature_vector: numeric features for the optional MoE multiclass.
        """
        opp_applied = base_log_odds is not None
        base = base_log_odds if base_log_odds is not None else _logit(self.base_pass_rate)

        contributions: dict[str, float] = {}
        total = base
        for name, value in signals.items():
            lr = self.lr_model.lookup(name, value)
            conf = 1.0 if signal_confidences is None else float(signal_confidences.get(name, 1.0))
            conf = min(max(conf, 0.0), 1.0)
            weighted = lr * conf
            contributions[name] = weighted
            total += weighted

        p_raw = float(sigmoid(total))
        calibrated = float(self.platt.transform_prob(p_raw))
        is_cal = self.platt.is_fitted
        predicted = PASS if calibrated >= 0.5 else RUN
        confidence = max(calibrated, 1.0 - calibrated)
        uncertainty = binary_entropy(calibrated)

        multiclass: dict[str, float] | None = None
        if self.moe is not None and self.moe.is_fitted and feature_vector is not None:
            multiclass = self.moe.predict_proba(feature_vector)

        return PredictionResult(
            p_pass_raw=p_raw,
            calibrated_prob=calibrated,
            logit_score=float(total),
            predicted_class=predicted,
            confidence=float(confidence),
            uncertainty=float(uncertainty),
            is_calibrated=bool(is_cal),
            base_log_odds=float(base),
            signal_contributions=contributions,
            opponent_prior_applied=opp_applied,
            multiclass=multiclass,
        )

    def fit(self, samples: list[tuple[dict[str, Any], str]]) -> BayesianEnsemble:
        """Fit the per-signal likelihood ratios from labelled plays."""
        self.lr_model.fit(samples)
        return self

    def calibrate(
        self,
        holdout: list[tuple[dict[str, Any], str]],
        *,
        base_log_odds_by_sample: list[float | None] | None = None,
    ) -> BayesianEnsemble:
        """Fit Platt scaling on held-out plays using raw decision scores."""
        scores: list[float] = []
        labels: list[int] = []
        for i, (signals, label) in enumerate(holdout):
            blo = None
            if base_log_odds_by_sample is not None:
                blo = base_log_odds_by_sample[i]
            res = self.predict(signals, base_log_odds=blo)
            scores.append(res.logit_score)
            labels.append(1 if label == PASS else 0)
        self.platt.fit(scores, labels)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "lr_model": self.lr_model.to_dict(),
            "platt": self.platt.to_dict(),
            "base_pass_rate": self.base_pass_rate,
            "moe": self.moe.to_dict() if self.moe is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BayesianEnsemble:
        return cls(
            lr_model=LikelihoodRatioModel.from_dict(d.get("lr_model", {})),
            platt=PlattScaler.from_dict(d.get("platt", {})),
            base_pass_rate=float(d.get("base_pass_rate", D1_NEUTRAL_PASS_RATE)),
            moe=MixtureOfExperts.from_dict(d["moe"]) if d.get("moe") else None,
        )


# ── Mixture of experts (multi-class refinement) ───────────────────────────────


@dataclass
class MixtureOfExperts:
    """Softmax-gated mixture for the 4-class play-type refinement.

    A small linear gating network produces mixture weights ``g_e(s)``; each
    expert is a linear softmax head over the play-type classes. Trained jointly
    by gradient descent (pure NumPy). Returns ``None`` until fitted so callers
    never receive a fabricated multi-class distribution.
    """

    gate_w: list[list[float]] | None = None  # (n_experts, n_features+1)
    expert_w: list[list[list[float]]] | None = None  # (n_experts, n_classes, n_features+1)
    labels: tuple[str, ...] = MULTICLASS_LABELS
    is_fitted: bool = False

    def fit(
        self,
        features: np.ndarray,
        class_indices: np.ndarray,
        *,
        n_experts: int = 2,
        lr: float = 0.1,
        epochs: int = 300,
        seed: int = 0,
    ) -> MixtureOfExperts:
        x = np.asarray(features, dtype=np.float64)
        y = np.asarray(class_indices, dtype=np.int64)
        if x.ndim != 2 or x.shape[0] != y.shape[0] or x.shape[0] == 0:
            raise ValueError("features must be (N, F) aligned with class_indices (N,)")
        n, f = x.shape
        c = len(self.labels)
        xb = np.hstack([x, np.ones((n, 1))])  # bias column
        rng = np.random.default_rng(seed)
        gate_w = rng.normal(scale=0.01, size=(n_experts, f + 1))
        expert_w = rng.normal(scale=0.01, size=(n_experts, c, f + 1))

        for _ in range(epochs):
            gate_logits = xb @ gate_w.T  # (n, e)
            gate = _softmax(gate_logits, axis=1)  # (n, e)
            # Expert class probabilities.
            expert_probs = np.stack(
                [_softmax(xb @ expert_w[e].T, axis=1) for e in range(n_experts)], axis=1
            )  # (n, e, c)

            onehot = np.zeros((n, c))
            onehot[np.arange(n), y] = 1.0

            # Responsibilities (posterior over experts given the true class):
            #   resp_e = gate_e * P_e(y_true) / Σ_e' gate_e' * P_e'(y_true)
            pe_true = expert_probs[np.arange(n), :, y]  # (n, e)
            mix_true = np.clip((gate * pe_true).sum(axis=1, keepdims=True), 1e-12, 1.0)
            resp = (gate * pe_true) / mix_true  # (n, e)

            # Gradient for experts: resp_e * (P_e - onehot).
            for e in range(n_experts):
                grad_e = (resp[:, e][:, None] * (expert_probs[:, e, :] - onehot)).T @ xb / n
                expert_w[e] -= lr * grad_e

            # Gradient for gate: (gate - resp).
            grad_g = (gate - resp).T @ xb / n
            gate_w -= lr * grad_g

        self.gate_w = gate_w.tolist()
        self.expert_w = expert_w.tolist()
        self.is_fitted = True
        return self

    def predict_proba(self, feature_vector: np.ndarray) -> dict[str, float]:
        if not self.is_fitted or self.gate_w is None or self.expert_w is None:
            raise RuntimeError("MixtureOfExperts is not fitted")
        x = np.asarray(feature_vector, dtype=np.float64).ravel()
        xb = np.concatenate([x, [1.0]])
        gate_w = np.asarray(self.gate_w)
        expert_w = np.asarray(self.expert_w)
        gate = _softmax(gate_w @ xb)  # (e,)
        probs = np.zeros(len(self.labels))
        for e in range(gate_w.shape[0]):
            probs += gate[e] * _softmax(expert_w[e] @ xb)
        probs = probs / probs.sum()
        return {label: float(p) for label, p in zip(self.labels, probs, strict=True)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_w": self.gate_w,
            "expert_w": self.expert_w,
            "labels": list(self.labels),
            "is_fitted": self.is_fitted,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MixtureOfExperts:
        return cls(
            gate_w=d.get("gate_w"),
            expert_w=d.get("expert_w"),
            labels=tuple(d.get("labels", MULTICLASS_LABELS)),
            is_fitted=bool(d.get("is_fitted", False)),
        )


def _softmax(z: np.ndarray, axis: int = -1) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64)
    z = z - np.max(z, axis=axis, keepdims=True)
    e = np.exp(z)
    return e / np.sum(e, axis=axis, keepdims=True)
