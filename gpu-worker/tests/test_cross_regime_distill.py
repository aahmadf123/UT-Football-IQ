"""Tests for practice<->game cross-regime self-distillation (Issue #150 §5.14).

Covers the play-call aligner + paired-play manifest, the soft-label KD loss math,
the mAP gate, the MLOps registration payload, the synthetic end-to-end flow, the
provenance checkpoint, and the regime-gated model-router registration. All run
torch-free / GPU-free / network-free — the real fine-tune lives behind a lazy
import and executes only on the GPU-worker host.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY

from pipeline import model_router
from pipeline.model_router import (
    DRONE_FOLLOW_DISTILLED,
    NIGHTLY_ONLY_VARIANTS,
    select_detect_variant,
)
from training.cross_regime_distill import (
    MIN_MAP_GAIN_PP,
    DistillConfig,
    DistillReport,
    build_checkpoint,
    build_registration_payload,
    build_teacher_logits,
    distillation_loss,
    improves_map,
    map50_proxy,
    run_synthetic,
)
from training.play_call_aligner import (
    DRONE_FOLLOW,
    FIXED_SIDELINE,
    MIN_PAIRED_PLAYS,
    USAGE_MARKER,
    align_plays,
    build_pairing_manifest,
    filter_validated,
    meets_min_plays,
)

# ── Fixtures / helpers ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_routing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENABLE_DRONE_DISTILL_NIGHTLY", raising=False)
    model_router.reload_routing()
    yield
    monkeypatch.delenv("ENABLE_DRONE_DISTILL_NIGHTLY", raising=False)
    model_router.reload_routing()


def _clip(
    clip_id: str,
    regime: str,
    *,
    play_call_id: str | None = "PLAY-A",
    result_state: str | None = "final",
    confidence: float | None = 0.8,
    video_id: str = "vid-1",
    **extra,
) -> dict:
    return {
        "id": clip_id,
        "video_id": video_id,
        "play_call_id": play_call_id,
        "capture_regime": regime,
        "result_state": result_state,
        "confidence": confidence,
        **extra,
    }


# ── Aligner: pairing ──────────────────────────────────────────────────────────


def test_align_pairs_game_and_practice_by_play_call_id():
    clips = [
        _clip("game-1", FIXED_SIDELINE),
        _clip("prac-1", DRONE_FOLLOW),
    ]
    pairs = align_plays(clips)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.play_call_id == "PLAY-A"
    # Teacher is always fixed_sideline (game), student always drone_follow.
    assert p.teacher.capture_regime == FIXED_SIDELINE
    assert p.teacher.clip_id == "game-1"
    assert p.student.capture_regime == DRONE_FOLLOW
    assert p.student.clip_id == "prac-1"


def test_align_ignores_clips_without_play_call_id():
    clips = [
        _clip("game-1", FIXED_SIDELINE, play_call_id=None),
        _clip("prac-1", DRONE_FOLLOW, play_call_id=None),
        _clip("game-2", FIXED_SIDELINE, play_call_id=""),
    ]
    assert align_plays(clips) == []


def test_align_skips_groups_missing_a_regime():
    # Two fixed_sideline clips, no drone_follow → cannot pair (two-regime guard).
    clips = [
        _clip("game-1", FIXED_SIDELINE),
        _clip("game-2", FIXED_SIDELINE),
    ]
    assert align_plays(clips) == []


def test_align_skips_duplicate_regime_in_group():
    clips = [
        _clip("game-1", FIXED_SIDELINE),
        _clip("prac-1", DRONE_FOLLOW),
        _clip("prac-2", DRONE_FOLLOW),  # ambiguous student → skip whole group
    ]
    assert align_plays(clips) == []


def test_align_groups_are_independent_by_play_call():
    clips = [
        _clip("g1", FIXED_SIDELINE, play_call_id="A"),
        _clip("p1", DRONE_FOLLOW, play_call_id="A"),
        _clip("g2", FIXED_SIDELINE, play_call_id="B"),
        _clip("p2", DRONE_FOLLOW, play_call_id="B"),
    ]
    pairs = align_plays(clips)
    assert {p.play_call_id for p in pairs} == {"A", "B"}


# ── Aligner: manifest preserves the required audit fields ─────────────────────


def test_manifest_preserves_all_required_fields():
    clips = [
        _clip(
            "game-1",
            FIXED_SIDELINE,
            result_state="final",
            confidence=0.91,
            model_version_id="mv-1",
            correction_source="human",
        ),
        _clip(
            "prac-1",
            DRONE_FOLLOW,
            result_state="preliminary",
            confidence=0.42,
            is_reviewed=True,
            model_version_id="mv-2",
        ),
    ]
    pairs = align_plays(clips)
    manifest = build_pairing_manifest(pairs, source="C:/Drone Footage")

    assert manifest["usage"] == USAGE_MARKER
    assert manifest["teacher_regime"] == FIXED_SIDELINE
    assert manifest["student_regime"] == DRONE_FOLLOW
    assert manifest["record_counts"]["paired_plays"] == 1

    rec = manifest["pairs"][0]
    for side in ("teacher", "student"):
        fields = rec[side]
        # The 8 acceptance-criteria fields are all present on every record.
        for key in (
            "video_id",
            "clip_id",
            "play_call_id",
            "capture_regime",
            "identity_confidence",
            "correction_source",
            "validation_status",
            "model_version_id",
        ):
            assert key in fields, f"{side} manifest record missing {key}"

    assert rec["teacher"]["identity_confidence"] == 0.91
    assert rec["teacher"]["correction_source"] == "human"
    assert rec["teacher"]["validation_status"] == "final"
    assert rec["student"]["validation_status"] == "preliminary"
    assert rec["student"]["model_version_id"] == "mv-2"


def test_validation_status_reviewed_when_no_result_state():
    clips = [
        _clip("g", FIXED_SIDELINE, result_state=None, is_reviewed=True),
        _clip("p", DRONE_FOLLOW, result_state=None, is_reviewed=False),
    ]
    p = align_plays(clips)[0]
    assert p.teacher.validation_status == "reviewed"
    assert p.student.validation_status == "unknown"


# ── Aligner: validated filter + min-plays gate ────────────────────────────────


def test_filter_validated_drops_unvalidated_pairs():
    validated = [
        _clip("g1", FIXED_SIDELINE, play_call_id="A", result_state="final"),
        _clip("p1", DRONE_FOLLOW, play_call_id="A", result_state="final"),
    ]
    unvalidated = [
        _clip("g2", FIXED_SIDELINE, play_call_id="B", result_state="preliminary"),
        _clip("p2", DRONE_FOLLOW, play_call_id="B", result_state="preliminary", is_reviewed=False),
    ]
    pairs = align_plays(validated + unvalidated)
    assert len(pairs) == 2
    kept = filter_validated(pairs)
    assert len(kept) == 1
    assert kept[0].play_call_id == "A"


def test_meets_min_plays_gate():
    one_pair = align_plays([_clip("g", FIXED_SIDELINE), _clip("p", DRONE_FOLLOW)])
    assert not meets_min_plays(one_pair)
    assert meets_min_plays(one_pair, minimum=1)
    assert MIN_PAIRED_PLAYS == 100


# ── KD loss math ──────────────────────────────────────────────────────────────


def test_distillation_loss_zero_when_distributions_match():
    logits = np.array([[2.0, 0.0, 0.0], [0.0, 3.0, 1.0]])
    # Pure soft term (alpha=1, no hard targets): identical logits → 0.
    assert distillation_loss(logits, logits, temperature=4.0, alpha=1.0) == pytest.approx(0.0)


def test_distillation_loss_positive_when_distributions_differ():
    student = np.array([[0.0, 0.0, 5.0]])
    teacher = np.array([[5.0, 0.0, 0.0]])
    loss = distillation_loss(student, teacher, temperature=2.0, alpha=1.0)
    assert loss > 0.0


def test_distillation_loss_hard_term_blends_with_alpha():
    student = np.array([[3.0, 0.0, 0.0]])
    teacher = np.array([[3.0, 0.0, 0.0]])
    # Soft term is 0 (match); a wrong hard target makes only the hard term fire,
    # scaled by (1 - alpha).
    full_hard = distillation_loss(student, teacher, np.array([2]), temperature=4.0, alpha=0.0)
    half = distillation_loss(student, teacher, np.array([2]), temperature=4.0, alpha=0.5)
    assert full_hard > 0.0
    assert half == pytest.approx(full_hard * 0.5)


def test_distillation_loss_is_deterministic():
    s = np.array([[1.0, 2.0, 0.5], [0.0, 1.0, 2.0]])
    t = np.array([[2.0, 1.0, 0.0], [1.0, 0.0, 3.0]])
    a = distillation_loss(s, t, temperature=3.0, alpha=0.7)
    b = distillation_loss(s, t, temperature=3.0, alpha=0.7)
    assert a == b


def test_distillation_loss_validates_inputs():
    s = np.zeros((2, 3))
    with pytest.raises(ValueError):
        distillation_loss(s, s, temperature=0.0)
    with pytest.raises(ValueError):
        distillation_loss(s, s, alpha=1.5)
    with pytest.raises(ValueError):
        distillation_loss(np.zeros((2, 3)), np.zeros((2, 4)))


def test_build_teacher_logits_encodes_class_and_confidence():
    dets = [
        {"class": "player", "confidence": 0.9},
        {"class": "ball", "confidence": 0.8},
    ]
    logits = build_teacher_logits(dets)
    assert logits.shape == (2, 3)
    # argmax row matches the detected class index (player=0, ball=1).
    assert int(np.argmax(logits[0])) == 0
    assert int(np.argmax(logits[1])) == 1
    assert np.isfinite(logits).all()  # confidences clipped away from 0/1 asymptotes


# ── mAP gate ──────────────────────────────────────────────────────────────────


def _report(baseline: float, student: float) -> DistillReport:
    return DistillReport(
        n_pairs=120,
        n_validated=120,
        baseline_map=baseline,
        student_map=student,
        epochs=1,
        final_loss=0.0,
    )


def test_improves_map_gate_boundary():
    assert improves_map(_report(0.50, 0.55))  # exactly +5 pp passes
    assert improves_map(_report(0.50, 0.60))
    assert not improves_map(_report(0.50, 0.549))  # +4.9 pp fails
    assert MIN_MAP_GAIN_PP == 5.0


def test_report_map_gain_pp_and_dict():
    r = _report(0.40, 0.52)
    assert r.map_gain_pp == pytest.approx(12.0)
    d = r.to_dict()
    assert d["map_gain_pp"] == pytest.approx(12.0)
    assert d["teacher_regime"] == FIXED_SIDELINE
    assert d["student_regime"] == DRONE_FOLLOW


def test_map50_proxy_perfect_and_empty():
    gt = [[[0.0, 0.0, 10.0, 10.0]]]
    perfect = [[{"bbox": [0.0, 0.0, 10.0, 10.0]}]]
    assert map50_proxy(perfect, gt) == pytest.approx(1.0)
    assert map50_proxy([[]], gt) == pytest.approx(0.0)
    assert map50_proxy([], []) == 0.0


# ── Registration payload ──────────────────────────────────────────────────────


def test_registration_payload_shape_and_regime_metadata():
    report = _report(0.55, 0.63)
    payload = build_registration_payload(
        report, version="distill-20260610", artifact_uri="r2://artifacts/x.pt"
    )
    assert payload["model_type"] == "detector"
    assert payload["model_name"] == DRONE_FOLLOW_DISTILLED
    assert payload["version"] == "distill-20260610"
    assert payload["artifact_uri"] == "r2://artifacts/x.pt"
    m = payload["metrics"]
    assert m["baseline_map"] == 0.55
    assert m["student_map"] == 0.63
    assert m["map_gain_pp"] == pytest.approx(8.0)
    assert m["teacher_regime"] == FIXED_SIDELINE
    assert m["student_regime"] == DRONE_FOLLOW
    assert m["meets_map_gate"] is True


# ── Checkpoint provenance ─────────────────────────────────────────────────────


def test_build_checkpoint_stamps_provenance_and_promotable():
    report = _report(0.50, 0.58)
    ck = build_checkpoint(report, source="synthetic", weights_uri=None, config=DistillConfig())
    assert ck["provenance"]["usage"] == USAGE_MARKER
    assert ck["provenance"]["teacher_regime"] == FIXED_SIDELINE
    assert ck["provenance"]["student_regime"] == DRONE_FOLLOW
    assert ck["promotable"] is True  # +8 pp clears the gate

    weak = build_checkpoint(
        _report(0.50, 0.52), source="synthetic", weights_uri=None, config=DistillConfig()
    )
    assert weak["promotable"] is False  # +2 pp does not


# ── Synthetic end-to-end (no torch) ───────────────────────────────────────────


def test_run_synthetic_produces_promotable_report_without_torch():
    assert "torch" not in sys.modules  # module import never pulled torch
    checkpoint, report = run_synthetic(n_pairs=120)
    assert report.n_pairs == 120
    assert report.student_map >= report.baseline_map
    assert report.map_gain_pp >= MIN_MAP_GAIN_PP
    assert checkpoint["promotable"] is True
    assert checkpoint["provenance"]["usage"] == USAGE_MARKER
    assert "torch" not in sys.modules


# ── Model-router registration of the distilled student ────────────────────────


def test_distilled_variant_is_nightly_only():
    assert DRONE_FOLLOW_DISTILLED in NIGHTLY_ONLY_VARIANTS
    assert model_router.is_nightly_only_variant(DRONE_FOLLOW_DISTILLED)


def test_select_detect_variant_gates_on_regime_and_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ENABLE_DRONE_DISTILL_NIGHTLY", "1")
    model_router.reload_routing()
    # Only nightly + drone_follow + flag yields the distilled student.
    assert select_detect_variant(NIGHTLY_PRIORITY, "drone_follow") == DRONE_FOLLOW_DISTILLED
    # Two-regime guard: fixed_sideline never gets the drone student.
    assert select_detect_variant(NIGHTLY_PRIORITY, "fixed_sideline") == "yolov8m"
    assert select_detect_variant(NIGHTLY_PRIORITY, None) == "yolov8m"
    # Same-session never gets a nightly-only variant.
    assert select_detect_variant(SAME_SESSION_PRIORITY, "drone_follow") == "yolov8n"


def test_select_detect_variant_falls_back_when_flag_off():
    # Flag off (fixture default) → baseline routing for every regime.
    assert select_detect_variant(NIGHTLY_PRIORITY, "drone_follow") == "yolov8m"
    assert select_detect_variant(SAME_SESSION_PRIORITY, "drone_follow") == "yolov8n"


def test_distilled_variant_blocked_from_same_session(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # An override that tries to put the distilled student in same-session must be
    # replaced by the default same-session variant (hard guardrail).
    cfg = tmp_path / "routing.json"
    cfg.write_text('{"detect": {"same_session": "yolov8m-drone-distilled", "nightly": "yolov8m"}}')
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    try:
        assert model_router.ROUTING["detect"]["same_session"] == "yolov8n"
    finally:
        monkeypatch.delenv("MODEL_ROUTING_CONFIG", raising=False)
        model_router.reload_routing()
