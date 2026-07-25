"""Tests for gpu-worker/pipeline/model_router.py (Issue #73).

Covers the stage-aware routing API that replaced Issue #16's
priority-only ``select_model(priority)``. Pose mapping must remain
identical so issue #16's downstream behaviour is preserved.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY

from pipeline import model_router
from pipeline.model_router import (
    DEFAULT_ROUTING,
    RTMPOSE_FAST,
    RTMPOSE_MEDIUM,
    UNKNOWN_STAGE_FALLBACK,
    build_routing_artifact,
    is_nightly,
    is_same_session,
    select_model,
)


@pytest.fixture(autouse=True)
def _reset_routing(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with the default routing table and no env override."""
    monkeypatch.delenv("MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("ENABLE_SAM3_NIGHTLY", raising=False)
    monkeypatch.delenv("ENABLE_BOTSORT_NIGHTLY", raising=False)
    monkeypatch.delenv("ENABLE_DRONE_DISTILL_NIGHTLY", raising=False)
    model_router.reload_routing()
    yield
    monkeypatch.delenv("MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("ENABLE_SAM3_NIGHTLY", raising=False)
    monkeypatch.delenv("ENABLE_BOTSORT_NIGHTLY", raising=False)
    monkeypatch.delenv("ENABLE_DRONE_DISTILL_NIGHTLY", raising=False)
    model_router.reload_routing()


# ── Pose preservation (Issue #16 contract) ────────────────────────────────────


def test_select_model_pose_same_session_returns_rtmpose_fast() -> None:
    assert select_model("pose", SAME_SESSION_PRIORITY) == RTMPOSE_FAST


def test_select_model_pose_nightly_returns_rtmpose_medium() -> None:
    assert select_model("pose", NIGHTLY_PRIORITY) == RTMPOSE_MEDIUM


def test_select_model_pose_high_priority_returns_fast() -> None:
    assert select_model("pose", SAME_SESSION_PRIORITY + 5) == RTMPOSE_FAST


def test_select_model_pose_just_below_threshold_returns_medium() -> None:
    assert select_model("pose", SAME_SESSION_PRIORITY - 1) == RTMPOSE_MEDIUM


def test_select_model_pose_negative_priority_returns_medium() -> None:
    assert select_model("pose", -5) == RTMPOSE_MEDIUM


# ── Other stages ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stage",
    ["segment", "calibrate", "detect", "ball", "track", "reid", "pose", "render", "embeddings"],
)
def test_select_model_returns_non_empty_string_for_known_stages(stage: str) -> None:
    fast = select_model(stage, SAME_SESSION_PRIORITY)
    nightly = select_model(stage, NIGHTLY_PRIORITY)
    assert isinstance(fast, str) and fast
    assert isinstance(nightly, str) and nightly


def test_detect_same_session_and_nightly_variants_differ() -> None:
    # The default detect routing uses a lighter YOLO for same-session.
    fast = select_model("detect", SAME_SESSION_PRIORITY)
    nightly = select_model("detect", NIGHTLY_PRIORITY)
    assert fast != nightly
    assert fast == DEFAULT_ROUTING["detect"]["same_session"]
    assert nightly == DEFAULT_ROUTING["detect"]["nightly"]


def test_select_model_returns_match_from_default_routing_table() -> None:
    for stage, variants in DEFAULT_ROUTING.items():
        assert select_model(stage, SAME_SESSION_PRIORITY) == variants["same_session"]
        assert select_model(stage, NIGHTLY_PRIORITY) == variants["nightly"]


# ── Unknown-stage fallback ────────────────────────────────────────────────────


def test_select_model_unknown_stage_returns_fallback_without_raising() -> None:
    assert select_model("does-not-exist", SAME_SESSION_PRIORITY) == UNKNOWN_STAGE_FALLBACK
    assert select_model("also-bogus", NIGHTLY_PRIORITY) == UNKNOWN_STAGE_FALLBACK


# ── build_routing_artifact ────────────────────────────────────────────────────


def test_build_routing_artifact_shape_for_pose() -> None:
    assert build_routing_artifact("pose", SAME_SESSION_PRIORITY) == {"pose": RTMPOSE_FAST}
    assert build_routing_artifact("pose", NIGHTLY_PRIORITY) == {"pose": RTMPOSE_MEDIUM}


def test_build_routing_artifact_for_unknown_stage() -> None:
    assert build_routing_artifact("ghost-stage", 0) == {"ghost-stage": UNKNOWN_STAGE_FALLBACK}


# ── Predicates ────────────────────────────────────────────────────────────────


def test_is_same_session_true_for_high_priority() -> None:
    assert is_same_session(SAME_SESSION_PRIORITY) is True
    assert is_same_session(SAME_SESSION_PRIORITY + 1) is True


def test_is_same_session_false_for_low_priority() -> None:
    assert is_same_session(SAME_SESSION_PRIORITY - 1) is False
    assert is_same_session(NIGHTLY_PRIORITY) is False


def test_is_nightly_true_for_zero_priority() -> None:
    assert is_nightly(NIGHTLY_PRIORITY) is True


def test_is_nightly_false_for_high_priority() -> None:
    assert is_nightly(SAME_SESSION_PRIORITY) is False


# ── Env-driven override via MODEL_ROUTING_CONFIG ──────────────────────────────


def test_routing_config_override_replaces_named_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {
                "detect": {"same_session": "custom-fast", "nightly": "custom-heavy"},
            }
        )
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()

    assert select_model("detect", SAME_SESSION_PRIORITY) == "custom-fast"
    assert select_model("detect", NIGHTLY_PRIORITY) == "custom-heavy"
    # Stages not mentioned in the override keep their defaults — pose unchanged.
    assert select_model("pose", SAME_SESSION_PRIORITY) == RTMPOSE_FAST
    assert select_model("pose", NIGHTLY_PRIORITY) == RTMPOSE_MEDIUM


def test_routing_config_partial_override_keeps_unmentioned_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only override the same_session bucket for detect — nightly should still
    # be the default.
    cfg = tmp_path / "routing.json"
    cfg.write_text(json.dumps({"detect": {"same_session": "yolov8s"}}))
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()

    assert select_model("detect", SAME_SESSION_PRIORITY) == "yolov8s"
    assert select_model("detect", NIGHTLY_PRIORITY) == DEFAULT_ROUTING["detect"]["nightly"]


def test_routing_config_missing_file_falls_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(tmp_path / "nope.json"))
    model_router.reload_routing()

    assert select_model("pose", SAME_SESSION_PRIORITY) == RTMPOSE_FAST


def test_routing_config_malformed_json_falls_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "routing.json"
    cfg.write_text("{not valid json")
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()

    assert select_model("pose", NIGHTLY_PRIORITY) == RTMPOSE_MEDIUM


def test_bundled_routing_json_matches_default_routing() -> None:
    """The in-tree JSON should mirror DEFAULT_ROUTING so the doc and the
    Python defaults never drift apart."""
    bundled = Path(__file__).resolve().parent.parent / "pipeline" / "model_routing.json"
    payload = json.loads(bundled.read_text())
    assert payload == DEFAULT_ROUTING


# ── Coverage sanity ───────────────────────────────────────────────────────────


def test_model_selection_covers_all_integer_priorities_for_pose() -> None:
    for p in range(-5, 20):
        model = select_model("pose", p)
        assert model in (RTMPOSE_FAST, RTMPOSE_MEDIUM), (
            f"select_model('pose', {p}) returned unexpected value: {model!r}"
        )


def test_reload_routing_returns_current_table() -> None:
    table = model_router.reload_routing()
    assert table is model_router.ROUTING
    assert "pose" in table


# ── SAM 3.1 nightly routing (Issue #74) ───────────────────────────────────────


def test_sam3_disabled_by_default_detect_nightly_is_yolov8m() -> None:
    assert select_model("detect", NIGHTLY_PRIORITY) == "yolov8m"
    assert select_model("track", NIGHTLY_PRIORITY) == "iou-tracker"


@pytest.mark.parametrize("flag_value", ["1", "true", "yes", "on", "TRUE"])
def test_sam3_nightly_env_flag_routes_detect_and_track(
    flag_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_SAM3_NIGHTLY", flag_value)
    model_router.reload_routing()
    assert select_model("detect", NIGHTLY_PRIORITY) == model_router.SAM3_1
    assert select_model("track", NIGHTLY_PRIORITY) == model_router.SAM3_MASK_TRACKER


def test_sam3_nightly_env_flag_does_not_change_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SAM3_NIGHTLY", "1")
    model_router.reload_routing()
    assert select_model("detect", SAME_SESSION_PRIORITY) == "yolov8n"
    assert select_model("track", SAME_SESSION_PRIORITY) == "iou-tracker"


def test_sam3_disabled_value_keeps_default_nightly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SAM3_NIGHTLY", "0")
    model_router.reload_routing()
    assert select_model("detect", NIGHTLY_PRIORITY) == "yolov8m"


def test_routing_config_cannot_force_sam3_into_same_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety guard: malicious / accidental config that puts SAM 3.1 in
    same_session must be rejected at load time."""
    cfg = tmp_path / "routing.json"
    cfg.write_text(json.dumps({
        "detect": {"same_session": "sam3.1", "nightly": "yolov8m"},
        "track": {"same_session": "sam3-mask-tracker", "nightly": "iou-tracker"},
    }))
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    # Forbidden variant must NOT be returned for same-session.
    detect_same = select_model("detect", SAME_SESSION_PRIORITY)
    track_same = select_model("track", SAME_SESSION_PRIORITY)
    assert detect_same not in model_router.NIGHTLY_ONLY_VARIANTS
    assert track_same not in model_router.NIGHTLY_ONLY_VARIANTS
    # And it reverts to the bundled defaults so the period-break window
    # is preserved.
    assert detect_same == DEFAULT_ROUTING["detect"]["same_session"]
    assert track_same == DEFAULT_ROUTING["track"]["same_session"]


def test_is_nightly_only_variant_flags_sam3() -> None:
    assert model_router.is_nightly_only_variant("sam3.1") is True
    assert model_router.is_nightly_only_variant("sam3-mask-tracker") is True
    assert model_router.is_nightly_only_variant("yolov8n") is False
    assert model_router.is_nightly_only_variant("iou-tracker") is False


# ── Calibrate routing (Issue #127) ───────────────────────────────────────────


def test_calibrate_same_session_is_lite_variant() -> None:
    assert select_model("calibrate", SAME_SESSION_PRIORITY) == model_router.CALIB_HOUGH_DLT


def test_calibrate_nightly_is_kalman_variant() -> None:
    assert select_model("calibrate", NIGHTLY_PRIORITY) == model_router.CALIB_HOUGH_DLT_KALMAN


def test_calibrate_variants_are_not_nightly_only_guardrail() -> None:
    # Both calibrate variants are pixel-only OpenCV paths — neither is on the
    # SAM/embeddings hard guardrail.
    assert not model_router.is_nightly_only_variant(model_router.CALIB_HOUGH_DLT)
    assert not model_router.is_nightly_only_variant(model_router.CALIB_HOUGH_DLT_KALMAN)


def test_build_routing_artifact_for_calibrate() -> None:
    assert build_routing_artifact("calibrate", SAME_SESSION_PRIORITY) == {
        "calibrate": model_router.CALIB_HOUGH_DLT
    }
    assert build_routing_artifact("calibrate", NIGHTLY_PRIORITY) == {
        "calibrate": model_router.CALIB_HOUGH_DLT_KALMAN
    }


# ── Ball routing (Issue #133) ────────────────────────────────────────────────


def test_ball_same_session_is_dedicated_nano_model() -> None:
    assert select_model("ball", SAME_SESSION_PRIORITY) == model_router.YOLO_BALL


def test_ball_nightly_is_dedicated_nano_model() -> None:
    # Same nano model in both buckets — SAHI is gated by regime, not priority.
    assert select_model("ball", NIGHTLY_PRIORITY) == model_router.YOLO_BALL


def test_ball_variant_is_not_nightly_only_guardrail() -> None:
    # The dedicated ball model is lightweight and same-session safe — it must
    # not be on the SAM/embeddings hard guardrail.
    assert not model_router.is_nightly_only_variant(model_router.YOLO_BALL)


def test_build_routing_artifact_for_ball() -> None:
    assert build_routing_artifact("ball", SAME_SESSION_PRIORITY) == {
        "ball": model_router.YOLO_BALL
    }
    assert build_routing_artifact("ball", NIGHTLY_PRIORITY) == {
        "ball": model_router.YOLO_BALL
    }


def test_ball_cannot_be_forced_off_guardrail_into_sam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override that points ball at a nightly-only SAM variant in the
    same-session bucket must be rejected at load time."""
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {"ball": {"same_session": "sam3.1", "nightly": model_router.YOLO_BALL}}
        )
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    ball_same = select_model("ball", SAME_SESSION_PRIORITY)
    assert ball_same not in model_router.NIGHTLY_ONLY_VARIANTS
    assert ball_same == DEFAULT_ROUTING["ball"]["same_session"]


# ── Embeddings nightly routing (Issue #8) ────────────────────────────────────


def test_embeddings_nightly_routes_to_baseline_variant() -> None:
    assert (
        select_model("embeddings", NIGHTLY_PRIORITY)
        == model_router.PLAY_EMBED_BASELINE
    )


def test_embeddings_same_session_returns_none_by_default() -> None:
    """Embeddings are nightly-only; same-session must remain inert."""
    assert select_model("embeddings", SAME_SESSION_PRIORITY) == "none"


def test_play_embed_baseline_is_nightly_only() -> None:
    assert model_router.is_nightly_only_variant(model_router.PLAY_EMBED_BASELINE)


def test_routing_config_cannot_force_embeddings_to_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malicious / accidental override that pins the heavy embedding
    encoder to the same-session bucket must be rejected at load time."""
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {
                "embeddings": {
                    "same_session": model_router.PLAY_EMBED_BASELINE,
                    "nightly": model_router.PLAY_EMBED_BASELINE,
                }
            }
        )
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    fast = select_model("embeddings", SAME_SESSION_PRIORITY)
    assert fast not in model_router.NIGHTLY_ONLY_VARIANTS
    assert fast == DEFAULT_ROUTING["embeddings"]["same_session"]


def test_build_routing_artifact_sam3_nightly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_SAM3_NIGHTLY", "1")
    model_router.reload_routing()
    assert build_routing_artifact("detect", NIGHTLY_PRIORITY) == {"detect": "sam3.1"}
    assert build_routing_artifact("track", NIGHTLY_PRIORITY) == {
        "track": "sam3-mask-tracker",
    }


# ── Tracker adapters: BoT-SORT / StrongSORT routing (Issue #129) ──────────────


def test_track_same_session_stays_iou_tracker_by_default() -> None:
    # The lightweight IoU tracker remains the same-session path (period-break).
    assert select_model("track", SAME_SESSION_PRIORITY) == model_router.IOU_TRACKER


def test_botsort_and_strongsort_are_nightly_only_guardrail() -> None:
    assert model_router.is_nightly_only_variant(model_router.BOTSORT)
    assert model_router.is_nightly_only_variant(model_router.STRONGSORT)


@pytest.mark.parametrize("flag_value", ["1", "true", "yes", "on", "TRUE"])
def test_enable_botsort_nightly_routes_nightly_track(
    flag_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_BOTSORT_NIGHTLY", flag_value)
    model_router.reload_routing()
    assert select_model("track", NIGHTLY_PRIORITY) == model_router.BOTSORT
    # Same-session is never upgraded — period-break window stays predictable.
    assert select_model("track", SAME_SESSION_PRIORITY) == model_router.IOU_TRACKER


def test_botsort_disabled_value_keeps_iou_nightly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_BOTSORT_NIGHTLY", "0")
    model_router.reload_routing()
    assert select_model("track", NIGHTLY_PRIORITY) == model_router.IOU_TRACKER


def test_sam3_takes_precedence_over_botsort_for_nightly_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When both flags are on, the SAM 3.1 mask tracker (tied to SAM 3.1 mask
    # detections) wins the nightly track slot.
    monkeypatch.setenv("ENABLE_BOTSORT_NIGHTLY", "1")
    monkeypatch.setenv("ENABLE_SAM3_NIGHTLY", "1")
    model_router.reload_routing()
    assert select_model("track", NIGHTLY_PRIORITY) == model_router.SAM3_MASK_TRACKER


def test_botsort_cannot_be_forced_into_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {"track": {"same_session": model_router.BOTSORT, "nightly": "iou-tracker"}}
        )
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    same = select_model("track", SAME_SESSION_PRIORITY)
    assert same not in model_router.NIGHTLY_ONLY_VARIANTS
    assert same == DEFAULT_ROUTING["track"]["same_session"]


def test_strongsort_selectable_for_nightly_via_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {"track": {"same_session": "iou-tracker", "nightly": model_router.STRONGSORT}}
        )
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    assert select_model("track", NIGHTLY_PRIORITY) == model_router.STRONGSORT
    # ...but it can never leak into same-session.
    assert select_model("track", SAME_SESSION_PRIORITY) == model_router.IOU_TRACKER


def test_build_routing_artifact_botsort_nightly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_BOTSORT_NIGHTLY", "1")
    model_router.reload_routing()
    assert build_routing_artifact("track", NIGHTLY_PRIORITY) == {"track": "botsort"}
    assert build_routing_artifact("track", SAME_SESSION_PRIORITY) == {
        "track": "iou-tracker"
    }


# ── Re-ID upgrade: PARSeq nightly / Tesseract same-session (Issue #131) ───────


def test_reid_same_session_is_tesseract_jersey_ocr() -> None:
    assert select_model("reid", SAME_SESSION_PRIORITY) == model_router.JERSEY_OCR


def test_reid_nightly_is_parseq_ocr() -> None:
    assert select_model("reid", NIGHTLY_PRIORITY) == model_router.PARSEQ_OCR


def test_parseq_ocr_is_nightly_only_guardrail() -> None:
    assert model_router.is_nightly_only_variant(model_router.PARSEQ_OCR)
    assert not model_router.is_nightly_only_variant(model_router.JERSEY_OCR)


def test_reid_cannot_force_parseq_into_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "routing.json"
    cfg.write_text(
        json.dumps(
            {"reid": {"same_session": model_router.PARSEQ_OCR, "nightly": model_router.PARSEQ_OCR}}
        )
    )
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(cfg))
    model_router.reload_routing()
    same = select_model("reid", SAME_SESSION_PRIORITY)
    assert same not in model_router.NIGHTLY_ONLY_VARIANTS
    assert same == DEFAULT_ROUTING["reid"]["same_session"]


def test_build_routing_artifact_for_reid() -> None:
    assert build_routing_artifact("reid", SAME_SESSION_PRIORITY) == {
        "reid": model_router.JERSEY_OCR
    }
    assert build_routing_artifact("reid", NIGHTLY_PRIORITY) == {
        "reid": model_router.PARSEQ_OCR
    }
