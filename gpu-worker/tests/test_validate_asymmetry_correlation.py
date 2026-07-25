"""Tests for the asymmetry-correlation validation harness (Issue #149).

Fixture-based proof that the harness itself is correct: a synthetic
30-player cohort whose trainer scores are a noisy linear function of the CV
asymmetry index must recover a high Pearson r. The real-world correlation
run happens on staff machines against real Toledo footage exports — see
docs/asymmetry-validation-runbook.md.
"""

import csv
import datetime as dt
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.validate_asymmetry_correlation import (
    evaluate,
    load_cv_metrics,
    load_trainer_reports,
    main,
    pair_players,
    pearson,
)

ASSESSMENT_DATE = dt.date(2026, 9, 12)


def _write_fixture(
    tmp_path: Path,
    n_players: int = 30,
    *,
    slope: float = 12.0,
    noise: float = 0.3,
    invert: bool = False,
) -> tuple[Path, Path]:
    """CV + trainer CSVs where trainer_score ≈ slope * (asym - 1) + noise."""
    rng = random.Random(42)
    cv_path = tmp_path / "cv_asymmetry.csv"
    trainer_path = tmp_path / "trainer_reports.csv"

    with cv_path.open("w", newline="") as cv_fh, trainer_path.open("w", newline="") as tr_fh:
        cv_writer = csv.writer(cv_fh)
        tr_writer = csv.writer(tr_fh)
        cv_writer.writerow(
            ["player_id", "clip_id", "clip_date", "asymmetry_index", "confidence", "sample_count"]
        )
        tr_writer.writerow(["player_id", "assessment_date", "trainer_asymmetry_score", "laterality"])

        for i in range(n_players):
            player_id = f"player-{i:03d}"
            base_asym = 1.0 + (i / max(n_players - 1, 1)) * 0.5  # 1.0 → 1.5
            for clip in range(3):
                clip_date = ASSESSMENT_DATE - dt.timedelta(days=clip * 2)
                jitter = rng.uniform(-0.02, 0.02)
                cv_writer.writerow(
                    [
                        player_id,
                        f"clip-{i}-{clip}",
                        clip_date.isoformat(),
                        round(base_asym + jitter, 4),
                        0.85,
                        8,
                    ]
                )
            score = slope * (base_asym - 1.0) + rng.uniform(-noise, noise)
            if invert:
                score = 6.0 - score
            tr_writer.writerow(
                [player_id, ASSESSMENT_DATE.isoformat(), round(score, 3), "L"]
            )

    return cv_path, trainer_path


# ── pearson ───────────────────────────────────────────────────────────────────


def test_pearson_perfect_positive() -> None:
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)


def test_pearson_perfect_negative() -> None:
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_undefined_for_constant_series() -> None:
    assert pearson([1, 1, 1], [2, 4, 6]) is None


# ── end-to-end recovery ───────────────────────────────────────────────────────


def test_recovers_planted_correlation_on_thirty_players(tmp_path: Path) -> None:
    cv_path, trainer_path = _write_fixture(tmp_path)

    pairs = pair_players(
        load_cv_metrics(cv_path),
        load_trainer_reports(trainer_path),
        window_days=7,
        min_samples_per_player=3,
    )
    results = evaluate(pairs, fail_under=0.4)

    assert results["sample_size"] == 30
    assert results["meets_sample_size"] is True
    assert results["pearson_r"] is not None
    assert results["pearson_r"] > 0.9
    assert results["passed"] is True


def test_cli_pass_exit_code_and_output(tmp_path: Path) -> None:
    cv_path, trainer_path = _write_fixture(tmp_path)
    out_path = tmp_path / "results.json"

    exit_code = main(
        [
            "--cv-metrics",
            str(cv_path),
            "--trainer-reports",
            str(trainer_path),
            "--output",
            str(out_path),
        ]
    )

    assert exit_code == 0
    assert out_path.exists()
    assert '"passed": true' in out_path.read_text()


def test_cli_fails_when_correlation_below_threshold(tmp_path: Path) -> None:
    cv_path, trainer_path = _write_fixture(tmp_path, invert=True)

    exit_code = main(["--cv-metrics", str(cv_path), "--trainer-reports", str(trainer_path)])

    assert exit_code == 1


def test_small_sample_is_inconclusive_not_failing(tmp_path: Path) -> None:
    cv_path, trainer_path = _write_fixture(tmp_path, n_players=10, invert=True)

    pairs = pair_players(
        load_cv_metrics(cv_path),
        load_trainer_reports(trainer_path),
        window_days=7,
        min_samples_per_player=3,
    )
    results = evaluate(pairs, fail_under=0.4)

    assert results["meets_sample_size"] is False
    assert results["passed"] is None
    # And the CLI must not fail the gate on an inconclusive sample.
    assert main(["--cv-metrics", str(cv_path), "--trainer-reports", str(trainer_path)]) == 0


# ── pairing rules ─────────────────────────────────────────────────────────────


def test_clips_outside_window_are_excluded(tmp_path: Path) -> None:
    cv_path = tmp_path / "cv.csv"
    trainer_path = tmp_path / "trainer.csv"
    stale_date = (ASSESSMENT_DATE - dt.timedelta(days=30)).isoformat()
    cv_path.write_text(
        "player_id,clip_id,clip_date,asymmetry_index,confidence,sample_count\n"
        + "".join(f"p1,c{i},{stale_date},1.4,0.9,8\n" for i in range(3))
    )
    trainer_path.write_text(
        "player_id,assessment_date,trainer_asymmetry_score,laterality\n"
        f"p1,{ASSESSMENT_DATE.isoformat()},5,L\n"
    )

    pairs = pair_players(
        load_cv_metrics(cv_path),
        load_trainer_reports(trainer_path),
        window_days=7,
        min_samples_per_player=3,
    )

    assert pairs == []


def test_confidence_weighting_prefers_confident_clips(tmp_path: Path) -> None:
    cv_path = tmp_path / "cv.csv"
    trainer_path = tmp_path / "trainer.csv"
    day = ASSESSMENT_DATE.isoformat()
    cv_path.write_text(
        "player_id,clip_id,clip_date,asymmetry_index,confidence,sample_count\n"
        f"p1,c1,{day},1.0,0.9,8\n"
        f"p1,c2,{day},1.0,0.9,8\n"
        f"p1,c3,{day},2.0,0.1,8\n"
    )
    trainer_path.write_text(
        "player_id,assessment_date,trainer_asymmetry_score,laterality\n"
        f"p1,{day},1,L\n"
    )

    pairs = pair_players(
        load_cv_metrics(cv_path),
        load_trainer_reports(trainer_path),
        window_days=7,
        min_samples_per_player=3,
    )

    assert len(pairs) == 1
    # Weighted mean sits far closer to the confident 1.0 clips than to 2.0.
    assert pairs[0]["cv_asymmetry_index"] < 1.2
