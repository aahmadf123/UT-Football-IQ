"""Tests for the Bayesian ensemble (Issue #136)."""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.play_prediction.bayesian_ensemble import (
    D1_NEUTRAL_PASS_RATE,
    BayesianEnsemble,
    LikelihoodRatioModel,
    MixtureOfExperts,
    PredictionResult,
)


def _toy_samples() -> list[tuple[dict[str, str], str]]:
    """Shotgun/empty/long → pass; i_form/short → run."""
    samples: list[tuple[dict[str, str], str]] = []
    for _ in range(40):
        samples.append(({"formation": "shotgun_empty", "backfield": "shotgun"}, "pass"))
        samples.append(({"formation": "i_form", "backfield": "under_center"}, "run"))
    return samples


def test_unfitted_ensemble_returns_base_rate() -> None:
    ens = BayesianEnsemble()
    res = ens.predict({"formation": "i_form"})
    # No likelihood evidence → probability equals the base pass rate.
    assert np.isclose(res.p_pass_raw, D1_NEUTRAL_PASS_RATE, atol=1e-6)
    assert not res.is_calibrated
    assert not res.opponent_prior_applied


def test_predict_returns_full_result_shape() -> None:
    ens = BayesianEnsemble().fit(_toy_samples())
    res = ens.predict({"formation": "shotgun_empty", "backfield": "shotgun"})
    assert isinstance(res, PredictionResult)
    d = res.to_dict()
    for key in (
        "p_pass_raw",
        "calibrated_prob",
        "logit_score",
        "predicted_class",
        "confidence",
        "uncertainty",
        "is_calibrated",
        "signal_contributions",
    ):
        assert key in d
    assert res.predicted_class in ("run", "pass")
    assert 0.0 <= res.calibrated_prob <= 1.0
    assert 0.0 <= res.uncertainty <= 1.0


def test_fitted_ensemble_separates_run_and_pass() -> None:
    ens = BayesianEnsemble().fit(_toy_samples())
    pass_res = ens.predict({"formation": "shotgun_empty", "backfield": "shotgun"})
    run_res = ens.predict({"formation": "i_form", "backfield": "under_center"})
    assert pass_res.predicted_class == "pass"
    assert run_res.predicted_class == "run"
    assert pass_res.calibrated_prob > run_res.calibrated_prob


def test_log_odds_combination_matches_formula() -> None:
    """logit = base + Σ log Λ_i (confidence-weighted)."""
    lr = LikelihoodRatioModel(
        log_lr={"formation": {"shotgun_empty": 1.2}, "backfield": {"shotgun": 0.8}},
        is_fitted=True,
    )
    ens = BayesianEnsemble(lr_model=lr)
    base = math.log(0.55 / 0.45)
    res = ens.predict({"formation": "shotgun_empty", "backfield": "shotgun"})
    expected_logit = base + 1.2 + 0.8
    assert np.isclose(res.logit_score, expected_logit, atol=1e-9)


def test_confidence_weighting_downweights_low_confidence_signal() -> None:
    lr = LikelihoodRatioModel(log_lr={"formation": {"shotgun_empty": 2.0}}, is_fitted=True)
    ens = BayesianEnsemble(lr_model=lr)
    full = ens.predict({"formation": "shotgun_empty"}, signal_confidences={"formation": 1.0})
    half = ens.predict({"formation": "shotgun_empty"}, signal_confidences={"formation": 0.5})
    assert full.signal_contributions["formation"] == pytest.approx(2.0)
    assert half.signal_contributions["formation"] == pytest.approx(1.0)
    assert full.calibrated_prob > half.calibrated_prob


def test_base_log_odds_overrides_default_and_sets_flag() -> None:
    ens = BayesianEnsemble()
    # Strongly run-leaning opponent prior.
    res = ens.predict({}, base_log_odds=math.log(0.2 / 0.8))
    assert res.opponent_prior_applied
    assert res.predicted_class == "run"
    assert res.calibrated_prob < 0.5


def test_calibration_sets_is_calibrated_flag() -> None:
    samples = _toy_samples()
    ens = BayesianEnsemble().fit(samples)
    assert not ens.predict(samples[0][0]).is_calibrated
    ens.calibrate(samples)
    assert ens.platt.is_fitted
    assert ens.predict(samples[0][0]).is_calibrated


def test_unknown_signal_value_contributes_zero() -> None:
    ens = BayesianEnsemble().fit(_toy_samples())
    # A formation value never seen in training contributes 0 log-LR.
    res = ens.predict({"formation": "never_seen_formation"})
    assert res.signal_contributions["formation"] == 0.0


def test_uncertainty_is_high_near_half_low_when_confident() -> None:
    ens = BayesianEnsemble().fit(_toy_samples())
    confident = ens.predict({"formation": "shotgun_empty", "backfield": "shotgun"})
    neutral = ens.predict({})
    assert neutral.uncertainty > confident.uncertainty


def test_ensemble_roundtrip_dict() -> None:
    ens = BayesianEnsemble().fit(_toy_samples())
    ens.calibrate(_toy_samples())
    restored = BayesianEnsemble.from_dict(ens.to_dict())
    a = ens.predict({"formation": "shotgun_empty"})
    b = restored.predict({"formation": "shotgun_empty"})
    assert np.isclose(a.calibrated_prob, b.calibrated_prob)


# ── Mixture of experts ────────────────────────────────────────────────────────


def test_moe_unfitted_raises_on_predict() -> None:
    moe = MixtureOfExperts()
    assert not moe.is_fitted
    with pytest.raises(RuntimeError):
        moe.predict_proba(np.zeros(4))


def test_moe_predict_proba_is_distribution() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(200, 4))
    # 4-class labels driven by feature 0.
    y = np.clip((x[:, 0] * 2 + 2).astype(int), 0, 3)
    moe = MixtureOfExperts().fit(x, y, epochs=100)
    probs = moe.predict_proba(x[0])
    assert set(probs.keys()) == set(moe.labels)
    assert np.isclose(sum(probs.values()), 1.0, atol=1e-6)
    assert all(0.0 <= v <= 1.0 for v in probs.values())


def test_moe_attached_to_ensemble_emits_multiclass() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(200, 4))
    y = np.clip((x[:, 0] * 2 + 2).astype(int), 0, 3)
    moe = MixtureOfExperts().fit(x, y, epochs=50)
    ens = BayesianEnsemble(moe=moe).fit(_toy_samples())
    res = ens.predict({"formation": "shotgun_empty"}, feature_vector=x[0])
    assert res.multiclass is not None
    assert np.isclose(sum(res.multiclass.values()), 1.0, atol=1e-6)


def test_ensemble_without_moe_has_none_multiclass() -> None:
    ens = BayesianEnsemble().fit(_toy_samples())
    res = ens.predict({"formation": "shotgun_empty"}, feature_vector=np.zeros(8))
    assert res.multiclass is None
