"""Tests for pre-snap pressure prediction (Issue #140)."""

from __future__ import annotations

import os
import sys
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import stage_pressure
from pipeline.pressure.classifier import PressureClassifier, PressureModel
from pipeline.pressure.feature_extractor import (
    PRESSURE_FEATURE_NAMES,
    extract_pressure_features,
)
from pipeline.spatial.feature_schema import FieldFrameError

LOS = 50.0


def _player(tid: str, x: float, y: float, side: str) -> dict[str, Any]:
    return {
        "id": tid,
        "side_of_ball": side,
        "track_points": [{"frame_number": 10, "field_x": x, "field_y": y}],
    }


def _ol() -> list[dict[str, Any]]:
    # 5 OL on the LOS, 1.5 yd apart.
    return [_player(f"ol{i}", LOS, -3.0 + 1.5 * i, "offense") for i in range(5)]


def _standard_front() -> list[dict[str, Any]]:
    """4 down linemen + 3 LBs at depth, balanced vs a 5-man protection."""
    tracks = _ol()
    # 4 DL in the gaps just across the LOS (defense advances toward -x here).
    for i, y in enumerate((-2.2, -0.7, 0.7, 2.2)):
        tracks.append(_player(f"dl{i}", LOS + 1.0, y, "defense"))
    # 3 LBs at ~4.5 yd depth.
    for i, y in enumerate((-4.0, 0.0, 4.0)):
        tracks.append(_player(f"lb{i}", LOS + 4.5, y, "defense"))
    return tracks


def _heavy_blitz_front() -> list[dict[str, Any]]:
    """5 down linemen + 2 LBs creeping to the LOS (blitz look)."""
    tracks = _ol()
    for i, y in enumerate((-3.0, -1.5, 0.0, 1.5, 3.0)):
        tracks.append(_player(f"dl{i}", LOS + 1.0, y, "defense"))
    for i, y in enumerate((-2.0, 2.0)):
        tracks.append(_player(f"lb{i}", LOS + 1.8, y, "defense"))  # within blitz depth
    return tracks


def _wide_ol_c_gap_front() -> list[dict[str, Any]]:
    tracks = [_player(f"ol{i}", LOS, y, "offense") for i, y in enumerate((-9.0, -4.5, 0.0, 4.5, 9.0))]
    tracks.append(_player("dl-c", LOS + 1.0, 6.7, "defense"))  # near wide interior gap -> C-gap
    return tracks


def test_field_frame_guard_blocks_uncalibrated() -> None:
    with pytest.raises(FieldFrameError):
        extract_pressure_features(
            _standard_front(), 10, LOS, calibration_confirmed=False
        )


def test_features_capture_gaps_dl_and_blitz() -> None:
    feats = extract_pressure_features(
        _standard_front(), 10, LOS, calibration_confirmed=True, down=2, distance_yards=8.0
    )
    assert feats.ol_count == 5
    assert len(feats.ol_gap_widths) == 4
    assert feats.mean_gap_width == pytest.approx(1.5, abs=0.01)
    assert feats.dl_count == 4
    assert feats.blitz_indicator is False  # LBs at 4.5 yd are not blitzing
    # Vector layout matches the declared schema length.
    assert feats.to_vector().shape == (len(PRESSURE_FEATURE_NAMES),)


def test_features_include_c_gap_count_in_vector_schema() -> None:
    feats = extract_pressure_features(_wide_ol_c_gap_front(), 10, LOS, calibration_confirmed=True)
    assert "dl_in_c_gap" in PRESSURE_FEATURE_NAMES
    c_idx = PRESSURE_FEATURE_NAMES.index("dl_in_c_gap")
    assert feats.dl_gap_counts["c"] >= 1
    assert feats.to_vector()[c_idx] >= 1.0


def test_blitz_front_flags_blitzers_and_more_rushers() -> None:
    standard = extract_pressure_features(_standard_front(), 10, LOS, calibration_confirmed=True)
    blitz = extract_pressure_features(_heavy_blitz_front(), 10, LOS, calibration_confirmed=True)
    assert blitz.blitz_indicator is True
    assert blitz.blitzers >= 1
    assert blitz.dl_count >= 5
    assert blitz.rusher_blocker_diff > standard.rusher_blocker_diff


def test_baseline_classifier_is_experimental_and_in_range() -> None:
    clf = PressureClassifier()  # no model loaded → deterministic baseline
    feats = extract_pressure_features(_standard_front(), 10, LOS, calibration_confirmed=True)
    out = clf.predict(feats)
    assert out.value in ("pressure", "no_pressure")
    assert 0.0 <= out.confidence <= 1.0
    assert out.is_calibrated is False
    assert out.experimental is True
    assert out.calibration_method == "uncalibrated"
    assert out.detail["model"] == "pressure-baseline-heuristic"


def test_baseline_pressure_increases_with_blitz() -> None:
    clf = PressureClassifier()
    p_standard = clf.predict(
        extract_pressure_features(_standard_front(), 10, LOS, calibration_confirmed=True)
    )
    p_blitz = clf.predict(
        extract_pressure_features(_heavy_blitz_front(), 10, LOS, calibration_confirmed=True)
    )
    prob_standard = p_standard.probabilities["pressure"]
    prob_blitz = p_blitz.probabilities["pressure"]
    assert prob_blitz > prob_standard


def test_trained_model_reports_calibrated() -> None:
    rng = np.random.default_rng(0)
    n = 200
    x = rng.normal(size=(n, len(PRESSURE_FEATURE_NAMES)))
    # Label correlated with the rusher_blocker_diff feature (index 4).
    logit = 1.5 * x[:, 4] - 0.3
    y = (rng.random(n) < 1.0 / (1.0 + np.exp(-logit))).astype(int)
    model = PressureModel(provenance={"usage": "unit-test"}).fit(x, y)
    model.calibrate(x, y)
    assert model.is_loaded
    assert model.is_calibrated

    clf = PressureClassifier(model=model)
    feats = extract_pressure_features(_heavy_blitz_front(), 10, LOS, calibration_confirmed=True)
    out = clf.predict(feats)
    assert out.calibration_method == "platt"
    assert out.is_calibrated is True


def test_model_checkpoint_roundtrip() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(50, len(PRESSURE_FEATURE_NAMES)))
    y = (x[:, 4] > 0).astype(int)
    model = PressureModel().fit(x, y)
    restored = PressureModel.from_checkpoint(model.to_checkpoint())
    assert restored.is_loaded
    assert restored.probability(x[0]) == pytest.approx(model.probability(x[0]), abs=1e-9)


# ── Stage-level behaviour ─────────────────────────────────────────────────────


def test_stage_suppresses_without_calibration() -> None:
    result = stage_pressure.run(
        "clip-1", _standard_front(), [{"event_type": "snap", "frame_number": 10}],
        analytics_safe=False, fps=30.0, job_id="job-1", persist=False,
    )
    assert result["suppressed"] is True
    assert result["pressure"]["reason"] == "calibration_not_analytics_safe"


def test_stage_suppresses_without_field_orientation() -> None:
    """Confirmed coordinates are not enough — the sign has to be measured too."""
    result = stage_pressure.run(
        "clip-1", _heavy_blitz_front(), [{"event_type": "snap", "frame_number": 10}],
        analytics_safe=True, fps=30.0, job_id="job-1", persist=False,
    )
    assert result["suppressed"] is True
    assert result["pressure"]["reason"] == "field_orientation_unresolved"


def test_stage_predicts_when_calibrated_offline() -> None:
    result = stage_pressure.run(
        "clip-1", _heavy_blitz_front(), [{"event_type": "snap", "frame_number": 10}],
        analytics_safe=True, fps=30.0, job_id="job-1", offense_direction=1,
        down=3, distance_yards=10.0, persist=False,
    )
    assert result["suppressed"] is False
    pressure = result["pressure"]
    assert pressure["pressure_prob"] is not None
    assert 0.0 <= pressure["pressure_prob"] <= 1.0
    # Experimental until validated (#140).
    assert pressure["experimental"] is True
    assert pressure["is_calibrated"] is False


def test_stage_persists_experimental_metric(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, Any] = {}

    def _fake_metric(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "metric-1"}

    monkeypatch.setattr(stage_pressure.backend, "create_metric", _fake_metric)
    result = stage_pressure.run(
        "clip-1", _heavy_blitz_front(), [{"event_type": "snap", "frame_number": 10}],
        analytics_safe=True, fps=30.0, job_id="job-1", offense_direction=1,
        persist=True,
    )
    assert result["metric_id"] == "metric-1"
    assert captured["metric_name"] == "pressure_prob"
    # Never written as trusted analytics.
    assert captured["experimental_flag"] is True
    assert captured["analytics_safe"] is False
