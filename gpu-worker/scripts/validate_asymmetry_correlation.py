"""Asymmetry-vs-trainer-report correlation validator (Issue #149).

Validates the acceptance criterion "asymmetry index correlates (>0.4
Pearson) with athletic-trainer reports on a 30-player sample" against REAL
Toledo footage outputs — never synthetic data.

OFFLINE TOOLING — this script:

  * is **never** in the request path, same-session, or nightly pipeline,
  * consumes two CSVs produced outside this repo (CV metric export + the
    athletic trainer's assessment sheet) and prints/writes a correlation
    report,
  * must be run where the footage-derived exports live (see
    ``docs/asymmetry-validation-runbook.md`` — the Toledo Drone Footage
    folder workflow); trainer reports are never committed to the repo.

Inputs
------

``--cv-metrics`` CSV (exported from persisted ``pose_stride_symmetry`` /
``workload_fusion`` metrics after processing real clips)::

    player_id,clip_id,clip_date,asymmetry_index,confidence,sample_count

``--trainer-reports`` CSV (athletic-trainer assessments; numeric severity
0-10 or an L/R ratio — no clinical free-text)::

    player_id,assessment_date,trainer_asymmetry_score,laterality

Pairing: for each trainer assessment, the confidence-weighted mean of the
player's CV asymmetry within ``--window-days`` of the assessment date.
Players with fewer than ``--min-samples-per-player`` CV rows are excluded.

Exit code: non-zero when the sample has >= 30 players and Pearson r falls
below ``--fail-under`` (default 0.4), so the staff run doubles as a gate.

Usage::

    python -m scripts.validate_asymmetry_correlation \
        --cv-metrics cv_asymmetry.csv \
        --trainer-reports trainer_reports.csv \
        --output results.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

REQUIRED_SAMPLE_SIZE = 30
DEFAULT_FAIL_UNDER = 0.4
DEFAULT_WINDOW_DAYS = 7
DEFAULT_MIN_SAMPLES = 3


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Plain Pearson correlation coefficient; None when undefined."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    sxx = sum(d * d for d in dx)
    syy = sum(d * d for d in dy)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum(a * b for a, b in zip(dx, dy, strict=True))
    return sxy / math.sqrt(sxx * syy)


def load_cv_metrics(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as fh:
        for raw in csv.DictReader(fh):
            date = _parse_date(raw.get("clip_date", ""))
            asym = _parse_float(raw.get("asymmetry_index"))
            if not raw.get("player_id") or date is None or asym is None:
                continue
            rows.append(
                {
                    "player_id": raw["player_id"].strip(),
                    "clip_date": date,
                    "asymmetry_index": asym,
                    "confidence": _parse_float(raw.get("confidence")) or 0.5,
                }
            )
    return rows


def load_trainer_reports(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as fh:
        for raw in csv.DictReader(fh):
            date = _parse_date(raw.get("assessment_date", ""))
            score = _parse_float(raw.get("trainer_asymmetry_score"))
            if not raw.get("player_id") or date is None or score is None:
                continue
            rows.append(
                {
                    "player_id": raw["player_id"].strip(),
                    "assessment_date": date,
                    "trainer_asymmetry_score": score,
                }
            )
    return rows


def pair_players(
    cv_rows: list[dict[str, Any]],
    trainer_rows: list[dict[str, Any]],
    *,
    window_days: int,
    min_samples_per_player: int,
) -> list[dict[str, Any]]:
    """One (cv_asymmetry, trainer_score) pair per player.

    Uses each player's most recent assessment; CV values are the
    confidence-weighted mean inside the +/- window around it.
    """
    cv_by_player: dict[str, list[dict[str, Any]]] = {}
    for row in cv_rows:
        cv_by_player.setdefault(row["player_id"], []).append(row)

    latest_assessment: dict[str, dict[str, Any]] = {}
    for row in trainer_rows:
        current = latest_assessment.get(row["player_id"])
        if current is None or row["assessment_date"] > current["assessment_date"]:
            latest_assessment[row["player_id"]] = row

    pairs: list[dict[str, Any]] = []
    for player_id, assessment in sorted(latest_assessment.items()):
        window = [
            r
            for r in cv_by_player.get(player_id, [])
            if abs((r["clip_date"] - assessment["assessment_date"]).days) <= window_days
        ]
        if len(window) < min_samples_per_player:
            continue
        total_weight = sum(r["confidence"] for r in window)
        if total_weight <= 0:
            continue
        weighted = sum(r["asymmetry_index"] * r["confidence"] for r in window) / total_weight
        pairs.append(
            {
                "player_id": player_id,
                "cv_asymmetry_index": round(weighted, 4),
                "trainer_asymmetry_score": assessment["trainer_asymmetry_score"],
                "cv_samples": len(window),
            }
        )
    return pairs


def evaluate(
    pairs: list[dict[str, Any]],
    *,
    fail_under: float,
) -> dict[str, Any]:
    r = pearson(
        [p["cv_asymmetry_index"] for p in pairs],
        [p["trainer_asymmetry_score"] for p in pairs],
    )
    sample_size = len(pairs)
    meets_sample = sample_size >= REQUIRED_SAMPLE_SIZE
    passed = r is not None and r > fail_under if meets_sample else None
    return {
        "pearson_r": round(r, 4) if r is not None else None,
        "sample_size": sample_size,
        "required_sample_size": REQUIRED_SAMPLE_SIZE,
        "threshold": fail_under,
        "meets_sample_size": meets_sample,
        # None = inconclusive (not enough players yet), not a failure.
        "passed": passed,
        "pairs": pairs,
        "note": (
            "Experimental sports-performance validation — CV asymmetry vs "
            "athletic-trainer assessments. Not a diagnosis."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cv-metrics", type=Path, required=True)
    parser.add_argument("--trainer-reports", type=Path, required=True)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--min-samples-per-player", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--fail-under", type=float, default=DEFAULT_FAIL_UNDER)
    parser.add_argument("--output", type=Path, default=None, help="Write JSON results here")
    args = parser.parse_args(argv)

    cv_rows = load_cv_metrics(args.cv_metrics)
    trainer_rows = load_trainer_reports(args.trainer_reports)
    pairs = pair_players(
        cv_rows,
        trainer_rows,
        window_days=args.window_days,
        min_samples_per_player=args.min_samples_per_player,
    )
    results = evaluate(pairs, fail_under=args.fail_under)

    print(f"players paired : {results['sample_size']} (need {REQUIRED_SAMPLE_SIZE})")
    print(f"pearson r      : {results['pearson_r']}")
    print(f"threshold      : > {results['threshold']}")
    if results["passed"] is None:
        print("verdict        : INCONCLUSIVE — sample below 30 players")
    else:
        print(f"verdict        : {'PASS' if results['passed'] else 'FAIL'}")

    if args.output is not None:
        args.output.write_text(json.dumps(results, indent=2))
        print(f"results written: {args.output}")

    return 1 if results["passed"] is False else 0


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


if __name__ == "__main__":
    sys.exit(main())
