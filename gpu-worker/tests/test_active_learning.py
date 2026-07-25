"""Tests for clip-level active-learning uncertainty aggregation (#145/#146)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.calibration import CalibratedOutput, ClipUncertainty, clip_uncertainty


def _calibrated(probs: dict[str, float]) -> CalibratedOutput:
    return CalibratedOutput.from_multiclass(
        probs, calibration_method="temperature", is_calibrated=True
    )


def _uncalibrated(probs: dict[str, float]) -> CalibratedOutput:
    return CalibratedOutput.from_multiclass(probs)


def test_empty_input_is_unscored_never_fabricated() -> None:
    out = clip_uncertainty({})
    assert out.score is None
    assert out.is_calibrated is False
    assert out.contributing == ()
    # Payload carries a NULL score, not a fabricated number.
    assert out.to_payload() == {"uncertainty_score": None, "uncertainty_calibrated": False}


def test_max_entropy_head_drives_clip_score() -> None:
    confident = _calibrated({"a": 0.95, "b": 0.05})  # low entropy
    uncertain = _calibrated({"x": 0.5, "y": 0.5})  # max entropy (1.0)
    out = clip_uncertainty({"formation": confident, "coverage": uncertain})
    # The least-sure head wins, not the average.
    assert out.score == pytest.approx(uncertain.entropy)
    assert out.score == pytest.approx(1.0)
    assert set(out.contributing) == {"formation", "coverage"}


def test_calibrated_only_when_all_heads_calibrated() -> None:
    mixed = clip_uncertainty(
        {"a": _calibrated({"p": 0.6, "q": 0.4}), "b": _uncalibrated({"p": 0.7, "q": 0.3})}
    )
    assert mixed.is_calibrated is False  # one uncalibrated head taints the clip

    all_cal = clip_uncertainty(
        {"a": _calibrated({"p": 0.6, "q": 0.4}), "b": _calibrated({"p": 0.7, "q": 0.3})}
    )
    assert all_cal.is_calibrated is True


def test_accepts_iterable_of_outputs() -> None:
    out = clip_uncertainty([_calibrated({"a": 0.5, "b": 0.5})])
    assert out.score == pytest.approx(1.0)
    assert out.is_calibrated is True
    assert len(out.contributing) == 1


def test_score_is_clamped_to_unit_interval() -> None:
    out = clip_uncertainty({"a": _calibrated({"a": 0.5, "b": 0.5})})
    assert 0.0 <= (out.score or 0.0) <= 1.0


def test_payload_shape_matches_clip_patch_contract() -> None:
    out = clip_uncertainty(
        {"coverage": _calibrated({"cover3": 0.34, "cover2": 0.33, "cover1": 0.33})}
    )
    payload = out.to_payload()
    assert set(payload) == {"uncertainty_score", "uncertainty_calibrated"}
    assert isinstance(out, ClipUncertainty)
    assert 0.0 <= payload["uncertainty_score"] <= 1.0
    assert payload["uncertainty_calibrated"] is True
