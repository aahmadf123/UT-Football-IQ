"""Coverage classifier over the shared spatial graph (Issue #139).

Architecture, honestly scoped to the worker's pure-NumPy footprint
(``torch_geometric`` is not a GPU-worker dependency):

* :class:`CoverageGNN` consumes a :class:`~pipeline.spatial.feature_schema.SpatialGraph`
  and runs **one message-passing round** — a motion/proximity-weighted mean
  aggregation of each defender's neighbours (the "edge block → node block" of
  the issue) — then a **mean+max graph readout** into a learnable softmax head
  over the 9 coverage classes (the "MLP output"). The head and an optional
  temperature calibrator are trained *offline* (``train_coverage_gnn.py``) and
  loaded from a checkpoint at runtime via ``COVERAGE_GNN_MODEL`` (no hard-coded
  path). A full PyTorch-Geometric 3-layer GNN is the documented offline upgrade
  path; its exported logits/weights load through the same checkpoint contract.

* :class:`DeterministicCoverageBaseline` is the always-available fallback: a
  pure heuristic over safety depth / box count / leverage that returns a valid
  9-class distribution. It generalises the prior ``stage_coverage`` shell
  heuristic so the stage never regresses when no checkpoint is present.

* :class:`CoverageClassifier` orchestrates the two and always returns a
  :class:`~pipeline.calibration.CalibratedOutput`: the GNN path is calibrated
  only when its checkpoint carried a fitted temperature scaler, otherwise the
  output is flagged ``is_calibrated=False`` / experimental so a raw softmax is
  never presented as a calibrated confidence (Issue #146).

Per the #164 contract, any coverage model trained on Big Data Bowl data is
**offline pretraining/evaluation only** until Toledo validation; the checkpoint
records its provenance and the classifier surfaces it in ``detail``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from pipeline.calibration.calibrated_output import (
    UNCALIBRATED,
    CalibratedOutput,
    TemperatureScaler,
)
from pipeline.spatial import feature_schema as fs

log = structlog.get_logger(__name__)

# The 9 coverage classes from Issue #139 §5.2 (exact taxonomy).
COVERAGE_CLASSES: tuple[str, ...] = (
    "cover_0",
    "cover_1",
    "cover_2_mof",
    "cover_2_shell",
    "cover_3",
    "cover_4",
    "man_free",
    "bracket_match",
    "cover_6",
)

# Env var pointing at an offline-trained checkpoint JSON. Unset → heuristic
# baseline (no hard-coded model path; matches REID_OCR_MODEL / PLAY_PREDICTION_*).
COVERAGE_GNN_MODEL_ENV = "COVERAGE_GNN_MODEL"

# Bust flag (Issue #139): post-snap alignment diverges > this from the pre-snap
# cluster within the reaction window below.
BUST_DIVERGENCE_YARDS = 3.0
BUST_WINDOW_SECONDS = 0.3

# Safety-depth thresholds (yards downfield of the LOS) for the heuristic.
_DEEP_YARDS = 10.0
_BOX_YARDS = 5.0

_EPS = 1e-12


def _softmax(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64)
    z = z - np.max(z)
    e = np.exp(z)
    return e / np.clip(e.sum(), _EPS, None)


# ── Graph embedding (deterministic, parameter-free) ───────────────────────────


def graph_embedding(graph: fs.SpatialGraph) -> np.ndarray:
    """Fixed message-passing readout → a graph-level feature vector.

    One round of proximity/motion-weighted neighbour mean-aggregation, then a
    concatenated mean+max pool over ``[x_i, neighbour_mean_i - x_i]``. Parameter
    free and deterministic, so it is identical whether a learned head or the
    heuristic consumes it. Output dim is ``4 * len(NODE_FEATURE_NAMES)``.
    """
    f = len(fs.NODE_FEATURE_NAMES)
    x = graph.node_features
    n = x.shape[0]
    if n == 0:
        return np.zeros(4 * f, dtype=np.float64)

    agg = np.zeros_like(x)
    wsum = np.zeros(n, dtype=np.float64)
    ei = graph.edge_index
    ef = graph.edge_features
    for e in range(ei.shape[1]):
        s = int(ei[0, e])
        d = int(ei[1, e])
        dist = float(ef[e, 0]) if ef.shape[0] else 0.0
        motion = float(ef[e, 3]) if ef.shape[1] >= 4 else 0.0
        # Proximity weight blended with motion similarity (both in #139's edges).
        w = (1.0 / (1.0 + dist)) * (0.5 + 0.5 * max(motion, 0.0))
        agg[d] += w * x[s]
        wsum[d] += w
    nonzero = wsum > _EPS
    agg[nonzero] /= wsum[nonzero, None]

    node_repr = np.concatenate([x, agg - x], axis=1)  # (N, 2F)
    pooled = np.concatenate([node_repr.mean(axis=0), node_repr.max(axis=0)])  # (4F,)
    return pooled


# ── Learnable graph model ─────────────────────────────────────────────────────


@dataclass
class CoverageGNN:
    """Graph readout + softmax head over the 9 coverage classes (Issue #139)."""

    weight: np.ndarray | None = None  # (9, D)
    bias: np.ndarray | None = None  # (9,)
    feat_mean: np.ndarray | None = None  # (D,)
    feat_std: np.ndarray | None = None  # (D,)
    temperature: TemperatureScaler | None = None
    labels: tuple[str, ...] = COVERAGE_CLASSES
    provenance: dict[str, Any] | None = None

    @property
    def is_loaded(self) -> bool:
        return self.weight is not None and self.bias is not None

    @property
    def is_calibrated(self) -> bool:
        return self.temperature is not None and self.temperature.is_fitted

    def _standardize(self, g: np.ndarray) -> np.ndarray:
        if self.feat_mean is None or self.feat_std is None:
            return g
        std = np.where(self.feat_std < _EPS, 1.0, self.feat_std)
        return (g - self.feat_mean) / std

    def logits(self, graph: fs.SpatialGraph) -> np.ndarray:
        if not self.is_loaded:
            raise RuntimeError("CoverageGNN has no weights loaded")
        g = self._standardize(graph_embedding(graph))
        assert self.weight is not None and self.bias is not None
        return self.weight @ g + self.bias

    def predict_proba(self, graph: fs.SpatialGraph) -> dict[str, float]:
        z = self.logits(graph)
        if self.temperature is not None and self.temperature.is_fitted:
            probs = self.temperature.transform_logits(z)
        else:
            probs = _softmax(z)
        return {lbl: float(p) for lbl, p in zip(self.labels, probs, strict=True)}

    # ── Training (offline use only) ────────────────────────────────────────────

    def fit(
        self,
        embeddings: np.ndarray,
        class_indices: np.ndarray,
        *,
        lr: float = 0.1,
        epochs: int = 400,
        l2: float = 1e-3,
        seed: int = 0,
    ) -> CoverageGNN:
        """Train the softmax head on pooled graph embeddings (multinomial LR)."""
        emb = np.asarray(embeddings, dtype=np.float64)
        y = np.asarray(class_indices, dtype=np.int64).ravel()
        if emb.ndim != 2 or emb.shape[0] != y.size or emb.shape[0] == 0:
            raise ValueError("embeddings must be (N, D) aligned with class_indices")
        c = len(self.labels)
        self.feat_mean = emb.mean(axis=0)
        self.feat_std = emb.std(axis=0)
        std = np.where(self.feat_std < _EPS, 1.0, self.feat_std)
        xs = (emb - self.feat_mean) / std

        n, d = xs.shape
        rng = np.random.default_rng(seed)
        w = rng.normal(scale=0.01, size=(c, d))
        b = np.zeros(c)
        onehot = np.zeros((n, c))
        onehot[np.arange(n), y] = 1.0
        for _ in range(epochs):
            logits = xs @ w.T + b
            logits -= logits.max(axis=1, keepdims=True)
            probs = np.exp(logits)
            probs /= np.clip(probs.sum(axis=1, keepdims=True), _EPS, None)
            grad = probs - onehot
            w -= lr * (grad.T @ xs / n + l2 * w)
            b -= lr * grad.mean(axis=0)
        self.weight = w
        self.bias = b
        return self

    def calibrate(
        self, embeddings: np.ndarray, class_indices: np.ndarray
    ) -> CoverageGNN:
        """Fit a temperature scaler on held-out embeddings (Issue #146)."""
        if not self.is_loaded:
            raise RuntimeError("fit the head before calibrating")
        emb = np.asarray(embeddings, dtype=np.float64)
        xs = self._standardize(emb)
        assert self.weight is not None and self.bias is not None
        logits = xs @ self.weight.T + self.bias
        self.temperature = TemperatureScaler().fit(logits, class_indices)
        return self

    # ── Serialisation ───────────────────────────────────────────────────────────

    def to_checkpoint(self) -> dict[str, Any]:
        if not self.is_loaded:
            raise RuntimeError("cannot serialise an unloaded CoverageGNN")
        assert self.weight is not None and self.bias is not None
        return {
            "schema_version": fs.SCHEMA_VERSION,
            "model": "coverage-gnn-numpy",
            "labels": list(self.labels),
            "weight": self.weight.tolist(),
            "bias": self.bias.tolist(),
            "feat_mean": None if self.feat_mean is None else self.feat_mean.tolist(),
            "feat_std": None if self.feat_std is None else self.feat_std.tolist(),
            "temperature": None if self.temperature is None else self.temperature.to_dict(),
            "provenance": self.provenance or {},
        }

    @classmethod
    def from_checkpoint(cls, d: dict[str, Any]) -> CoverageGNN:
        version = int(d.get("schema_version", -1))
        if version != fs.SCHEMA_VERSION:
            log.warning(
                "coverage_gnn_checkpoint_schema_mismatch",
                checkpoint_version=version,
                expected=fs.SCHEMA_VERSION,
            )
        temp = d.get("temperature")
        return cls(
            weight=np.asarray(d["weight"], dtype=np.float64),
            bias=np.asarray(d["bias"], dtype=np.float64),
            feat_mean=None if d.get("feat_mean") is None else np.asarray(d["feat_mean"], dtype=np.float64),
            feat_std=None if d.get("feat_std") is None else np.asarray(d["feat_std"], dtype=np.float64),
            temperature=TemperatureScaler.from_dict(temp) if temp else None,
            labels=tuple(d.get("labels", COVERAGE_CLASSES)),
            provenance=d.get("provenance", {}),
        )

    @classmethod
    def load_from_env(cls, env_var: str = COVERAGE_GNN_MODEL_ENV) -> CoverageGNN | None:
        """Load a checkpoint from the path in ``env_var`` (None when absent/bad)."""
        path_str = os.environ.get(env_var)
        if not path_str:
            return None
        path = Path(path_str)
        if not path.is_file():
            log.warning("coverage_gnn_checkpoint_missing", path=path_str)
            return None
        try:
            return cls.from_checkpoint(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("coverage_gnn_checkpoint_unreadable", path=path_str, error=str(exc))
            return None


# ── Deterministic heuristic baseline (always available) ───────────────────────


@dataclass
class DeterministicCoverageBaseline:
    """Heuristic 9-class coverage distribution from defender alignment.

    Not learned — generalises the prior ``stage_coverage`` shell logic into soft
    scores over all 9 classes. Always uncalibrated (experimental).
    """

    labels: tuple[str, ...] = COVERAGE_CLASSES
    temperature: float = 1.2

    def predict_proba(self, nodes: list[fs.PlayerNode]) -> dict[str, float]:
        if not nodes:
            return {lbl: 1.0 / len(self.labels) for lbl in self.labels}

        depths = np.array([nd.depth_behind_los for nd in nodes], dtype=np.float64)
        ys = np.array([nd.field_y for nd in nodes], dtype=np.float64)
        deep_mask = depths > _DEEP_YARDS
        deep = int(deep_mask.sum())
        box = int((depths < _BOX_YARDS).sum())
        # Lateral spread of the deep shell distinguishes MOF-open vs MOF-closed.
        deep_ys = ys[deep_mask]
        deep_middle = int(np.sum(np.abs(deep_ys) < 4.0)) if deep_ys.size else 0
        deep_spread = float(deep_ys.max() - deep_ys.min()) if deep_ys.size >= 2 else 0.0
        # Leverage cue: clustered inside leverage hints man/bracket.
        leverages = np.array([nd.leverage for nd in nodes], dtype=np.float64)
        inside_press = int(np.sum((leverages < -0.5) & (depths < _BOX_YARDS)))

        score = {lbl: 0.0 for lbl in self.labels}
        if deep == 0:
            score["cover_0"] += 2.0
            score["man_free"] += 0.5
        elif deep == 1:
            score["cover_1"] += 1.6
            score["man_free"] += 1.2 if box >= 5 else 0.6
            score["cover_3"] += 0.4
        elif deep == 2:
            if deep_middle >= 1 and deep_spread < 8.0:
                score["cover_2_mof"] += 1.6
            score["cover_2_shell"] += 1.4
            score["bracket_match"] += 0.4
        elif deep == 3:
            score["cover_3"] += 1.6
            score["cover_6"] += 0.6 if deep_spread > 18.0 else 0.2
        else:  # deep >= 4
            score["cover_4"] += 1.6
            score["cover_6"] += 0.5
        score["bracket_match"] += 0.4 * inside_press

        raw = np.array([score[lbl] for lbl in self.labels], dtype=np.float64)
        probs = _softmax(raw / max(self.temperature, _EPS))
        return {lbl: float(p) for lbl, p in zip(self.labels, probs, strict=True)}


# ── Orchestrator ──────────────────────────────────────────────────────────────


@dataclass
class CoverageClassifier:
    """Build the spatial graph and classify coverage with calibrated uncertainty."""

    gnn: CoverageGNN | None = None
    baseline: DeterministicCoverageBaseline | None = None

    @classmethod
    def from_env(cls) -> CoverageClassifier:
        return cls(gnn=CoverageGNN.load_from_env(), baseline=DeterministicCoverageBaseline())

    def classify(
        self,
        defender_nodes: list[fs.PlayerNode],
        graph: fs.SpatialGraph,
    ) -> CalibratedOutput:
        """Return a calibrated coverage prediction for one snap."""
        baseline = self.baseline or DeterministicCoverageBaseline()
        deep = int(sum(1 for nd in defender_nodes if nd.depth_behind_los > _DEEP_YARDS))
        box = int(sum(1 for nd in defender_nodes if nd.depth_behind_los < _BOX_YARDS))
        detail: dict[str, Any] = {
            "defender_count": len(defender_nodes),
            "deep_safeties": deep,
            "box_defenders": box,
        }

        if self.gnn is not None and self.gnn.is_loaded:
            probs = self.gnn.predict_proba(graph)
            calibrated = self.gnn.is_calibrated
            method = "temperature" if calibrated else UNCALIBRATED
            detail["model"] = "coverage-gnn-numpy"
            detail["provenance"] = self.gnn.provenance or {}
        else:
            probs = baseline.predict_proba(defender_nodes)
            calibrated = False
            method = UNCALIBRATED
            detail["model"] = "coverage-baseline-heuristic"

        return CalibratedOutput.from_multiclass(
            probs,
            calibration_method=method,
            is_calibrated=calibrated,
            detail=detail,
        )


# ── Bust flag (Issue #139) ────────────────────────────────────────────────────


def coverage_bust_flag(
    defender_tracklets: list[dict[str, Any]],
    snap_frame: int,
    fps: float,
    *,
    divergence_yards: float = BUST_DIVERGENCE_YARDS,
    window_seconds: float = BUST_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Detect a coverage bust: alignment diverging from the pre-snap cluster.

    Fires when any defender moves more than ``divergence_yards`` from its
    snap position within ``window_seconds`` — the rapid, large post-snap
    displacement that marks a blown rotation/assignment (Issue #139).
    """
    window = max(1, int(round(window_seconds * max(fps, 1.0))))
    target = snap_frame + window
    max_disp = 0.0
    busted_track: str | None = None
    measured = 0
    for t in defender_tracklets:
        a = fs.position_at_frame(t, snap_frame)
        b = fs.position_at_frame(t, target)
        if a is None or b is None:
            continue
        measured += 1
        disp = math.dist(a, b)
        if disp > max_disp:
            max_disp = disp
            busted_track = str(t.get("id", "unknown"))
    flag = max_disp > divergence_yards
    return {
        "coverage_bust_flag": bool(flag),
        "max_displacement_yards": round(max_disp, 2),
        "divergence_threshold_yards": divergence_yards,
        "window_frames": window,
        "defenders_measured": measured,
        "busted_track_id": busted_track if flag else None,
    }
