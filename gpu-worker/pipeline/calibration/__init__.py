"""Calibrated-output contract (Issue #146) for Phase-CV classifiers.

Issue #146 ("calibrated uncertainty everywhere") asks every classification
output to ship a calibrated confidence and the calibration method used, never a
bare softmax presented as if it were calibrated. This package provides the
:class:`~pipeline.calibration.calibrated_output.CalibratedOutput` wrapper that
the coverage GNN (#139) and pressure predictor (#140) emit.

The underlying calibration math (Platt / isotonic scaling, ECE, Brier,
reliability curves) already exists and is battle-tested in
``pipeline.play_prediction.calibration``; it is re-exported here so there is a
single implementation rather than a fork. New cross-cutting helpers
(multi-class temperature scaling, the ``CalibratedOutput`` envelope) live in
``calibrated_output``.
"""

from __future__ import annotations

from pipeline.calibration.active_learning import ClipUncertainty, clip_uncertainty
from pipeline.calibration.calibrated_output import (
    CalibratedOutput,
    TemperatureScaler,
    multiclass_ece,
)

# Re-export the existing, tested calibration primitives so callers have one
# import surface (no second implementation of Platt/isotonic/ECE).
from pipeline.play_prediction.calibration import (
    IsotonicCalibrator,
    PlattScaler,
    binary_entropy,
    brier_score,
    expected_calibration_error,
    reliability_curve,
    sigmoid,
)

__all__ = [
    "CalibratedOutput",
    "ClipUncertainty",
    "TemperatureScaler",
    "clip_uncertainty",
    "multiclass_ece",
    "IsotonicCalibrator",
    "PlattScaler",
    "binary_entropy",
    "brier_score",
    "expected_calibration_error",
    "reliability_curve",
    "sigmoid",
]
