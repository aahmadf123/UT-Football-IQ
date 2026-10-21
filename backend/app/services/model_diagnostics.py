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


def heldout_cases(
    rows: list[dict[str, Any]],
    predicted: list[float],
    baseline: float,
    *,
    source_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Case identity stays in the frozen holdout; no heldout fitting or qualification."""
    import hashlib
    import json

    if not rows or len(rows) != len(predicted):
        raise ValueError("Case diagnostics require the complete paired holdout")
    cases = []
    identities = set()
    for row, value in zip(rows, predicted, strict=True):
        if row.get("split") != "holdout":
            raise ValueError("Case diagnostics accept only immutable holdout rows")
        lineage = row["lineage"]
        clock = row["observation_clock"]
        if type(clock["timestamp_seconds"]) not in (int, float) or not math.isfinite(
            clock["timestamp_seconds"]
        ):
            raise ValueError("Case clock must retain a finite source timestamp")
        identity = {
            "task": row["task"],
            "lineage": lineage,
            "observation_clock": {**clock, "timestamp_seconds": float(clock["timestamp_seconds"])},
            "subject": row["subject"],
        }
        play = (row["task"], lineage["canonical_play_id"])
        if play in identities:
            raise ValueError("Case diagnostics cannot count alternate views as independent plays")
        identities.add(play)
        observed = row["target"]["value"]
        if any(
            type(v) not in (int, float) or not math.isfinite(v) for v in (value, observed, baseline)
        ):
            raise ValueError("Case diagnostics require actual finite observations and predictions")
        value, observed, baseline = float(value), float(observed), float(baseline)
        group = lineage.get("game_id") or lineage["session_id"]
        categories = {
            entry.get("category")
            for entry in source_groups or []
            if entry.get("group_id") == group
            and entry.get("independence_confirmed") is True
            and isinstance(entry.get("reviewer_ids"), list)
            and entry["reviewer_ids"]
            and all(isinstance(v, str) and v.strip() for v in entry["reviewer_ids"])
            and lineage["recording_id"] in entry.get("recording_ids", [])
            and lineage["media_sha256"] in entry.get("media_sha256", [])
            and entry.get("category") in {"practice", "coach_game", "television"}
        }
        if len(categories) > 1:
            raise ValueError("Conflicting confirmed source categories")
        case_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        binary = row["task"] in {"completion", "pressure"}
        edges = [-0.5, -0.1, 0, 0.1, 0.5] if binary else [-10, -5, -2, -1, 0, 1, 2, 5, 10]
        bounds = [None, *edges, None]
        lower, upper = next(
            (lo, hi)
            for lo, hi in zip(bounds[:-1], bounds[1:], strict=True)
            if (lo is None or value - observed >= lo) and (hi is None or value - observed < hi)
        )
        error_bin = (
            f"[{lower if lower is not None else '-inf'},{upper if upper is not None else 'inf'})"
        )
        outcome_pair = f"{int(observed)} → {int(value >= 0.5)}" if binary else "not_applicable"
        cases.append(
            {
                **identity,
                "case_id": case_id,
                "split": "holdout",
                "feature_version": row["feature_version"],
                "capture_regime": row.get("capture_regime", "unknown"),
                "source_group": group,
                "film_category": next(iter(categories)) if categories else "unknown",
                "observed_outcome": str(int(observed))
                if row["task"] in {"completion", "pressure"}
                else "not_applicable",
                "observed": observed,
                "predicted": value,
                "baseline": baseline,
                "error_bin": error_bin,
                "outcome_pair": outcome_pair,
                "error": value - observed,
                "baseline_error": baseline - observed,
                "feature_evidence_sha256": sorted(
                    {f["evidence_sha256"] for f in row["features"].values()}
                ),
                "annotation_sha256": row["target"]["annotation_sha256"],
            }
        )
    cases.sort(key=lambda case: case["case_id"])
    return case_document(cases)


def case_document(cases: list[dict[str, Any]]) -> dict[str, Any]:
    import hashlib
    import json

    dimensions = (
        "capture_regime",
        "source_group",
        "film_category",
        "observed_outcome",
        "error_bin",
        "outcome_pair",
    )
    groups = {
        dimension: [
            {"value": value, "case_count": sum(c[dimension] == value for c in cases)}
            for value in sorted({c[dimension] for c in cases})
        ]
        for dimension in dimensions
    }
    return {
        "schema_version": 1,
        "split": "holdout",
        "case_count": len(cases),
        "sha256": hashlib.sha256(
            json.dumps(cases, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "groups": groups,
        "cases": cases,
    }
