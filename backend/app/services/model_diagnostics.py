"""Descriptive heldout diagnostics from actual paired targets and predictions."""

import math
from typing import Any


def confusion_matrix(truth: list[str], predicted: list[str]) -> dict[str, Any]:
    if not truth or len(truth) != len(predicted):
        raise ValueError("Diagnostics require paired heldout observations")
    labels = sorted(set(truth) | set(predicted))
    return {
        "labels": labels,
        "counts": [
            [
                sum(t == row and p == column for t, p in zip(truth, predicted, strict=True))
                for column in labels
            ]
            for row in labels
        ],
        "rows": "observed",
        "columns": "predicted",
    }


def outcome_diagnostics(truth: list[float], predicted: list[float], binary: bool) -> dict[str, Any]:
    if (
        not truth
        or len(truth) != len(predicted)
        or any(
            isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
            for v in [*truth, *predicted]
        )
    ):
        raise ValueError("Diagnostics require finite paired heldout observations")
    edges = [-0.5, -0.1, 0, 0.1, 0.5] if binary else [-10, -5, -2, -1, 0, 1, 2, 5, 10]
    residuals = [p - t for t, p in zip(truth, predicted, strict=True)]
    bounds = [None, *edges, None]
    result = {
        "diagnostics_version": 1,
        "error_distribution": {
            "definition": "prediction_minus_observation",
            "unit": "probability" if binary else "yd",
            "interval": "lower_inclusive_upper_exclusive",
            "bins": [
                {
                    "lower": lower,
                    "upper": upper,
                    "count": sum(
                        (lower is None or value >= lower) and (upper is None or value < upper)
                        for value in residuals
                    ),
                }
                for lower, upper in zip(bounds[:-1], bounds[1:], strict=True)
            ],
        },
    }
    if binary:
        if any(t not in (0, 1) for t in truth) or any(not 0 <= p <= 1 for p in predicted):
            raise ValueError(
                "Binary diagnostics require probability predictions and binary targets"
            )
        result["confusion_matrix"] = {
            **confusion_matrix(
                [str(int(t)) for t in truth], [str(int(p >= 0.5)) for p in predicted]
            ),
            "decision_threshold": 0.5,
        }
    return result
