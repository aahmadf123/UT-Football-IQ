"""Tests for clip-level active-learning uncertainty aggregation (#145/#146)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace
from unittest.mock import patch

from pipeline import backend as orchestrator_backend
from pipeline import orchestrator
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


# ── Wiring: the score has to reach the clip, or the queue stays empty ─────────


class TestUncertaintyReachesTheClip:
    """``clip_uncertainty`` was tested and never called.

    ``clips.uncertainty_score`` was therefore never written, and the review
    queue orders by that column -- so every clip came back ``reason="unscored"``
    and sorted last. The queue looked empty because nothing had ever scored
    anything, not because the model was confident.
    """

    def test_extracts_both_heads_from_stage_artifacts(self) -> None:
        heads = orchestrator._uncertainty_heads(
            {
                "coverage": {"uncertainty": 0.4, "is_calibrated": True,
                             "coverage_confidence": 0.8},
                "pressure": {"pressure": {"uncertainty": 0.9, "is_calibrated": True,
                                          "confidence": 0.6}},
            }
        )
        assert set(heads) == {"coverage", "pressure"}
        assert heads["pressure"].entropy == pytest.approx(0.9)

    def test_pressure_entropy_is_read_from_its_nested_metric(self) -> None:
        # stage_pressure returns {"pressure": {**CalibratedOutput.to_dict()}},
        # so a flat lookup finds nothing and the head silently vanishes.
        heads = orchestrator._uncertainty_heads(
            {"pressure": {"pressure": {"uncertainty": 0.7, "is_calibrated": False}}}
        )
        assert heads["pressure"].entropy == pytest.approx(0.7)

    def test_a_head_with_no_distribution_contributes_nothing(self) -> None:
        # The uncalibrated fallback reports uncertainty=None. Coercing that to
        # 0.0 would read as "completely certain" and bury the clip.
        heads = orchestrator._uncertainty_heads(
            {"coverage": {"uncertainty": None, "is_calibrated": False}}
        )
        assert heads == {}

    def test_missing_stages_are_not_an_error(self) -> None:
        assert orchestrator._uncertainty_heads({}) == {}

    def test_the_max_head_drives_the_score(self) -> None:
        # Uncertainty sampling: the head the model is least sure about decides
        # review. Averaging would let confident heads mask one very unsure head.
        heads = orchestrator._uncertainty_heads(
            {
                "coverage": {"uncertainty": 0.1, "is_calibrated": True},
                "pressure": {"pressure": {"uncertainty": 0.95, "is_calibrated": True}},
            }
        )
        assert clip_uncertainty(heads).score == pytest.approx(0.95)

    def test_the_score_is_patched_to_the_backend(self) -> None:
        ctx = SimpleNamespace(
            clip_artifacts={"c1": {"coverage": {"uncertainty": 0.6, "is_calibrated": True}}}
        )
        with patch.object(orchestrator_backend, "patch_clip_uncertainty") as patched:
            orchestrator._score_clip_uncertainty(ctx, "c1")
        patched.assert_called_once()
        assert patched.call_args.args[1] == pytest.approx(0.6)

    def test_an_unscorable_clip_patches_none_rather_than_zero(self) -> None:
        ctx = SimpleNamespace(clip_artifacts={"c1": {}})
        with patch.object(orchestrator_backend, "patch_clip_uncertainty") as patched:
            orchestrator._score_clip_uncertainty(ctx, "c1")
        assert patched.call_args.args[1] is None

    def test_the_backend_client_skips_a_null_score(self) -> None:
        # The column means "not scored yet" and PATCH ignores nulls, so sending
        # one is a wasted round trip that reads like an attempt to clear it.
        with patch.object(orchestrator_backend, "_client") as client:
            orchestrator_backend.patch_clip_uncertainty("c1", None, False)
        client.assert_not_called()

    def test_a_zero_confidence_head_is_not_treated_as_missing(self) -> None:
        # `a or b` reads a legitimate 0.0 -- no confidence at all, which is a
        # real answer -- as absent, and silently substitutes the next key.
        heads = orchestrator._uncertainty_heads(
            {"coverage": {"uncertainty": 0.5, "confidence": 0.0,
                          "coverage_confidence": 0.9, "is_calibrated": True}}
        )
        assert heads["coverage"].confidence == pytest.approx(0.0)

    def test_it_falls_back_only_when_the_key_is_absent(self) -> None:
        heads = orchestrator._uncertainty_heads(
            {"coverage": {"uncertainty": 0.5, "coverage_confidence": 0.9,
                          "is_calibrated": True}}
        )
        assert heads["coverage"].confidence == pytest.approx(0.9)

    def test_no_confidence_key_at_all_is_zero(self) -> None:
        heads = orchestrator._uncertainty_heads(
            {"coverage": {"uncertainty": 0.5, "is_calibrated": False}}
        )
        assert heads["coverage"].confidence == pytest.approx(0.0)
