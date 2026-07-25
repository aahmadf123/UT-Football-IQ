"""Probability calibration for the pre-snap prediction ensemble (Issue #136).

Two calibrators, both pure-NumPy (``scipy`` / ``scikit-learn`` are deliberately
**not** GPU-worker dependencies, mirroring the rest of the pipeline):

* :class:`PlattScaler` — 1-D logistic regression ``P_cal = sigmoid(a*f + b)``
  fitted by Newton's method on a held-out score/label set. This is the
  calibrator named in the issue math.
* :class:`IsotonicCalibrator` — monotonic, non-parametric calibration via the
  pool-adjacent-violators algorithm (PAVA). Useful when the reliability curve
  is not sigmoidal.

Plus the calibration *diagnostics* the MLOps dashboard ships per Issue #136 and
the calibrated-uncertainty contract (Issue #146): Brier score, expected
calibration error (ECE), and a reliability curve.

Nothing here fabricates data: an unfitted calibrator is the identity transform
and reports ``is_fitted == False`` so callers never present an uncalibrated
probability as a calibrated one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

_EPS = 1e-12


def sigmoid(z: np.ndarray | float) -> np.ndarray:
    """Numerically-stable logistic function."""
    z = np.asarray(z, dtype=np.float64)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    exp_z = np.exp(z[~pos])
    out[~pos] = exp_z / (1.0 + exp_z)
    return out


def _clip01(p: np.ndarray) -> np.ndarray:
    return np.clip(p, _EPS, 1.0 - _EPS)


# ── Platt scaling ─────────────────────────────────────────────────────────────


@dataclass
class PlattScaler:
    """Logistic calibration ``sigmoid(a * score + b)``.

    ``score`` is the ensemble's raw decision value (its logit). When unfitted
    the scaler passes a *probability* through unchanged (identity), so it is
    always safe to apply.
    """

    a: float = 1.0
    b: float = 0.0
    is_fitted: bool = False

    def fit(
        self,
        scores: np.ndarray | list[float],
        labels: np.ndarray | list[int],
        *,
        max_iter: int = 100,
        tol: float = 1e-7,
    ) -> PlattScaler:
        """Fit ``a``/``b`` by Newton-Raphson on the binary cross-entropy.

        ``scores`` are real-valued decision scores (logits); ``labels`` are
        0/1 (1 == pass). Uses Platt's recommended target smoothing so the fit
        does not saturate on small held-out sets.
        """
        x = np.asarray(scores, dtype=np.float64).ravel()
        y = np.asarray(labels, dtype=np.float64).ravel()
        if x.size == 0 or x.size != y.size:
            raise ValueError("scores and labels must be non-empty and equal length")

        # Platt target smoothing (avoids 0/1 targets overfitting tiny sets).
        n_pos = float(np.sum(y == 1))
        n_neg = float(np.sum(y == 0))
        hi = (n_pos + 1.0) / (n_pos + 2.0)
        lo = 1.0 / (n_neg + 2.0)
        t = np.where(y == 1, hi, lo)

        a, b = 0.0, 0.0
        for _ in range(max_iter):
            p = _clip01(sigmoid(a * x + b))
            w = p * (1.0 - p)
            grad_a = float(np.sum((p - t) * x))
            grad_b = float(np.sum(p - t))
            h_aa = float(np.sum(w * x * x)) + 1e-9
            h_bb = float(np.sum(w)) + 1e-9
            h_ab = float(np.sum(w * x))
            det = h_aa * h_bb - h_ab * h_ab
            if abs(det) < 1e-15:
                break
            da = (h_bb * grad_a - h_ab * grad_b) / det
            db = (h_aa * grad_b - h_ab * grad_a) / det
            a -= da
            b -= db
            if abs(da) < tol and abs(db) < tol:
                break

        self.a, self.b, self.is_fitted = a, b, True
        return self

    def transform_score(self, score: float | np.ndarray) -> np.ndarray:
        """Map a raw decision score to a calibrated probability."""
        return sigmoid(self.a * np.asarray(score, dtype=np.float64) + self.b)

    def transform_prob(self, prob: float | np.ndarray) -> np.ndarray:
        """Map an *already-probability* input through the scaler.

        When unfitted this is the identity, so the ensemble can always call it.
        When fitted it re-scales via the probability's logit.
        """
        p = _clip01(np.asarray(prob, dtype=np.float64))
        if not self.is_fitted:
            return p
        logit = np.log(p / (1.0 - p))
        return self.transform_score(logit)

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "is_fitted": self.is_fitted, "kind": "platt"}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlattScaler:
        return cls(a=float(d.get("a", 1.0)), b=float(d.get("b", 0.0)), is_fitted=bool(d.get("is_fitted", False)))


# ── Isotonic regression (PAVA) ────────────────────────────────────────────────


@dataclass
class IsotonicCalibrator:
    """Monotone calibration via pool-adjacent-violators.

    Stores breakpoints ``(x_thresholds, y_values)`` and linearly interpolates
    between them. Identity when unfitted.
    """

    x_thresholds: list[float] | None = None
    y_values: list[float] | None = None
    is_fitted: bool = False

    def fit(self, probs: np.ndarray | list[float], labels: np.ndarray | list[int]) -> IsotonicCalibrator:
        x = np.asarray(probs, dtype=np.float64).ravel()
        y = np.asarray(labels, dtype=np.float64).ravel()
        if x.size == 0 or x.size != y.size:
            raise ValueError("probs and labels must be non-empty and equal length")

        order = np.argsort(x, kind="mergesort")
        xs = x[order]
        ys = y[order]

        # PAVA: maintain blocks of (sum, weight) enforcing non-decreasing means.
        block_sum = list(ys)
        block_w = [1.0] * len(ys)
        block_x = list(xs)
        i = 0
        while i < len(block_sum) - 1:
            mean_i = block_sum[i] / block_w[i]
            mean_j = block_sum[i + 1] / block_w[i + 1]
            if mean_i <= mean_j:
                i += 1
                continue
            # Merge block i+1 into i and back up.
            block_sum[i] += block_sum[i + 1]
            block_w[i] += block_w[i + 1]
            block_x[i] = block_x[i + 1]  # right edge of the pooled block
            del block_sum[i + 1]
            del block_w[i + 1]
            del block_x[i + 1]
            if i > 0:
                i -= 1

        self.x_thresholds = [float(v) for v in block_x]
        self.y_values = [float(s / w) for s, w in zip(block_sum, block_w, strict=True)]
        self.is_fitted = True
        return self

    def transform_prob(self, prob: float | np.ndarray) -> np.ndarray:
        p = _clip01(np.asarray(prob, dtype=np.float64))
        if not self.is_fitted or not self.x_thresholds or not self.y_values:
            return p
        xt = np.asarray(self.x_thresholds, dtype=np.float64)
        yv = np.asarray(self.y_values, dtype=np.float64)
        return np.interp(p, xt, yv, left=yv[0], right=yv[-1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "x_thresholds": self.x_thresholds,
            "y_values": self.y_values,
            "is_fitted": self.is_fitted,
            "kind": "isotonic",
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IsotonicCalibrator:
        return cls(
            x_thresholds=d.get("x_thresholds"),
            y_values=d.get("y_values"),
            is_fitted=bool(d.get("is_fitted", False)),
        )


# ── Diagnostics (MLOps dashboard + Issue #146 calibrated uncertainty) ─────────


def brier_score(probs: np.ndarray | list[float], labels: np.ndarray | list[int]) -> float:
    """Mean squared error between predicted P(pass) and the 0/1 outcome."""
    p = np.asarray(probs, dtype=np.float64).ravel()
    y = np.asarray(labels, dtype=np.float64).ravel()
    if p.size == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


def reliability_curve(
    probs: np.ndarray | list[float],
    labels: np.ndarray | list[int],
    n_bins: int = 10,
) -> list[dict[str, float | int]]:
    """Equal-width reliability bins for a reliability diagram.

    Each bin reports the mean predicted probability, the empirical accuracy,
    and the count. Empty bins are omitted so the dashboard never plots a
    fabricated point.
    """
    p = np.asarray(probs, dtype=np.float64).ravel()
    y = np.asarray(labels, dtype=np.float64).ravel()
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out: list[dict[str, float | int]] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (p >= lo) & (p < hi) if hi < 1.0 else (p >= lo) & (p <= hi)
        count = int(np.sum(mask))
        if count == 0:
            continue
        out.append(
            {
                "bin_lower": float(lo),
                "bin_upper": float(hi),
                "mean_predicted": float(np.mean(p[mask])),
                "empirical_accuracy": float(np.mean(y[mask])),
                "count": count,
            }
        )
    return out


def expected_calibration_error(
    probs: np.ndarray | list[float],
    labels: np.ndarray | list[int],
    n_bins: int = 10,
) -> float:
    """ECE = Σ (n_b / N) · |acc_b − conf_b| over equal-width bins."""
    p = np.asarray(probs, dtype=np.float64).ravel()
    if p.size == 0:
        return float("nan")
    curve = reliability_curve(p, labels, n_bins=n_bins)
    n = float(p.size)
    return float(
        sum(
            (b["count"] / n) * abs(b["empirical_accuracy"] - b["mean_predicted"])
            for b in curve
        )
    )


def binary_entropy(prob: float) -> float:
    """Shannon entropy (bits) of a Bernoulli(p) — the uncertainty signal.

    1.0 at p=0.5 (maximally uncertain), 0.0 at p∈{0,1}. This is the calibrated
    uncertainty surfaced alongside every prediction (Issue #146).
    """
    p = float(min(max(prob, _EPS), 1.0 - _EPS))
    return float(-(p * np.log2(p) + (1.0 - p) * np.log2(1.0 - p)))
