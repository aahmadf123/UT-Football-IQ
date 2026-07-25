"""Tests for gpu-worker/pipeline/segment_models.py and stage_segment.py (Issue #75).

Covers the play-boundary segmenter adapter pattern: every adapter
returns the same boundary schema, the factory falls back to the stub
on unknown / unavailable variants, ``stage_segment`` routes through
the adapter while preserving the clip artifact contract, and the
no-boundary edge case writes a single clip spanning the whole video.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import segment_models, stage_segment
from pipeline.segment_models import (
    LEARNED_PLAY,
    OPTICAL_FLOW,
    SOURCE_LEARNED,
    SOURCE_OPTICAL_FLOW,
    SOURCE_STUB,
    SPORTSBD,
    STUB_SEGMENTER,
    LearnedPlaySegmenter,
    OpticalFlowSegmenter,
    SegmenterBase,
    SportsBDSegmenter,
    StubSegmenter,
    get_segmenter,
    validate_boundary,
)

# ── Schema invariants ─────────────────────────────────────────────────────────


def _assert_boundary_schema(b: dict[str, Any]) -> None:
    assert validate_boundary(b), f"invalid boundary: {b}"
    assert isinstance(b["time_s"], float)
    assert b["time_s"] >= 0.0
    assert isinstance(b["confidence"], float)
    assert 0.0 <= b["confidence"] <= 1.0
    assert isinstance(b["source"], str) and b["source"]


def test_validate_boundary_accepts_valid_dict() -> None:
    assert validate_boundary({"time_s": 1.0, "confidence": 0.5, "source": "x"})


def test_validate_boundary_rejects_out_of_range_confidence() -> None:
    assert not validate_boundary({"time_s": 1.0, "confidence": 1.5, "source": "x"})
    assert not validate_boundary({"time_s": 1.0, "confidence": -0.1, "source": "x"})


def test_validate_boundary_rejects_missing_fields() -> None:
    assert not validate_boundary({"time_s": 1.0, "confidence": 0.5})
    assert not validate_boundary({})
    assert not validate_boundary("not a dict")  # type: ignore[arg-type]


def test_validate_boundary_rejects_negative_time() -> None:
    assert not validate_boundary({"time_s": -0.1, "confidence": 0.5, "source": "x"})


# ── Stub segmenter ────────────────────────────────────────────────────────────


def test_stub_segmenter_returns_periodic_boundaries(tmp_path: Path) -> None:
    seg = StubSegmenter(duration_s=120.0, period_s=30.0, confidence=0.42)
    out = seg.segment(tmp_path / "fake.mp4")
    assert [round(b["time_s"], 2) for b in out] == [30.0, 60.0, 90.0]
    for b in out:
        _assert_boundary_schema(b)
        assert b["source"] == SOURCE_STUB
        assert b["confidence"] == pytest.approx(0.42)


def test_stub_segmenter_no_boundaries_edge_case(tmp_path: Path) -> None:
    seg = StubSegmenter(no_boundaries=True)
    assert seg.segment(tmp_path / "fake.mp4") == []


def test_stub_segmenter_short_clip_yields_no_boundaries(tmp_path: Path) -> None:
    # period exceeds duration → no internal boundaries possible
    seg = StubSegmenter(duration_s=10.0, period_s=30.0)
    assert seg.segment(tmp_path / "fake.mp4") == []


def test_stub_segmenter_does_not_open_video(tmp_path: Path) -> None:
    # No file exists at this path — adapter must still succeed.
    out = StubSegmenter(duration_s=60.0, period_s=20.0).segment(
        tmp_path / "missing.mp4"
    )
    assert len(out) == 2


# ── Optical flow & learned segmenter (using shared quiet-frame helpers) ───────


def _fake_quiet_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    quiet: list[tuple[int, float]],
    fps: float = 30.0,
    duration: float = 120.0,
) -> None:
    def fake(_video_path: Path) -> tuple[list[tuple[int, float]], float, float]:
        return quiet, fps, duration

    monkeypatch.setattr(segment_models, "_quiet_frames_and_fps", fake)


def test_optical_flow_segmenter_emits_midpoint_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A single long quiet gap from frame 300..360 at 30 fps → midpoint
    # 330 / 30 = 11.0 s. Each sampled frame is 1 s apart given SAMPLE_FPS=2
    # but the helper here just feeds raw (idx, mag) tuples.
    quiet = [(i, 0.5) for i in range(300, 361, 15)]
    _fake_quiet_pipeline(monkeypatch, quiet)
    seg = OpticalFlowSegmenter()
    out = seg.segment(tmp_path / "video.mp4")
    assert len(out) == 1
    _assert_boundary_schema(out[0])
    assert out[0]["source"] == SOURCE_OPTICAL_FLOW
    assert out[0]["time_s"] == pytest.approx(11.0)
    # Confidence must be model-derived, not the old flat 0.7 placeholder.
    assert 0.0 < out[0]["confidence"] <= 1.0


def test_optical_flow_segmenter_no_quiet_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_quiet_pipeline(monkeypatch, [])
    assert OpticalFlowSegmenter().segment(tmp_path / "v.mp4") == []


def test_optical_flow_confidence_higher_for_longer_quieter_gaps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    short_noisy = [(i, 1.4) for i in range(100, 121, 15)]
    long_quiet = [(i, 0.1) for i in range(200, 321, 15)]
    _fake_quiet_pipeline(monkeypatch, short_noisy + long_quiet)
    out = OpticalFlowSegmenter().segment(tmp_path / "v.mp4")
    assert len(out) == 2
    out.sort(key=lambda b: b["time_s"])
    assert out[0]["confidence"] < out[1]["confidence"]


def test_learned_play_segmenter_applies_duration_prior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two gaps: one at ~25 s (matches play prior), one at ~3 s (too close
    # — likely a false positive). The second should be either dropped or
    # have lower confidence than the first.
    near = [(i, 0.4) for i in range(60, 121, 15)]      # ~3 s mark
    typical = [(i, 0.4) for i in range(720, 781, 15)]  # ~25 s mark
    _fake_quiet_pipeline(monkeypatch, near + typical)
    out = LearnedPlaySegmenter(min_confidence=0.0).segment(tmp_path / "v.mp4")
    assert all(b["source"] == SOURCE_LEARNED for b in out)
    assert len(out) == 2
    out.sort(key=lambda b: b["time_s"])
    # The boundary that matches the play-duration prior should be
    # weighted higher than a too-soon boundary.
    assert out[1]["confidence"] >= out[0]["confidence"]


def test_learned_play_segmenter_filters_below_min_confidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Very short, very noisy gap → low score → filtered out.
    quiet = [(i, 1.45) for i in range(60, 70, 5)]
    _fake_quiet_pipeline(monkeypatch, quiet)
    assert LearnedPlaySegmenter(min_confidence=0.9).segment(tmp_path / "v.mp4") == []


# ── SportsBD adapter ──────────────────────────────────────────────────────────


def test_sportsbd_segmenter_raises_when_package_missing() -> None:
    # The sportsbd package is intentionally not a project dependency
    # (it's a benchmark spike, see docs/segment-benchmark.md). The
    # adapter must surface a clean ImportError so the factory can fall
    # back to the stub.
    if "sportsbd" in sys.modules:  # pragma: no cover - depends on env
        pytest.skip("sportsbd actually installed in this env")
    with pytest.raises(ImportError):
        SportsBDSegmenter()


def test_sportsbd_segmenter_works_with_fake_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import types

    fake = types.ModuleType("sportsbd")

    def detect_shot_boundaries(_path: str, threshold: float = 0.5):
        return [{"time_s": 12.5, "score": 0.9}, 30.0]

    fake.detect_shot_boundaries = detect_shot_boundaries  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sportsbd", fake)

    seg = SportsBDSegmenter()
    out = seg.segment(tmp_path / "v.mp4")
    assert len(out) == 2
    for b in out:
        _assert_boundary_schema(b)
        assert b["source"] == "sportsbd"
    assert out[0]["time_s"] == pytest.approx(12.5)
    assert out[1]["time_s"] == pytest.approx(30.0)


# ── Factory routing ───────────────────────────────────────────────────────────


def test_get_segmenter_default_is_optical_flow() -> None:
    assert isinstance(get_segmenter(None), OpticalFlowSegmenter)
    assert isinstance(get_segmenter(""), OpticalFlowSegmenter)
    assert isinstance(get_segmenter(OPTICAL_FLOW), OpticalFlowSegmenter)
    assert isinstance(get_segmenter("optical-flow-fast"), OpticalFlowSegmenter)


def test_get_segmenter_learned_returns_learned() -> None:
    assert isinstance(get_segmenter(LEARNED_PLAY), LearnedPlaySegmenter)


def test_get_segmenter_stub_returns_stub() -> None:
    assert isinstance(get_segmenter(STUB_SEGMENTER), StubSegmenter)


def test_get_segmenter_unknown_returns_stub() -> None:
    assert isinstance(get_segmenter("nope-not-real"), StubSegmenter)


def test_get_segmenter_sportsbd_falls_back_to_stub_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(self: Any, *args: Any, **kwargs: Any) -> None:
        raise ImportError("sportsbd missing for test")

    monkeypatch.setattr(SportsBDSegmenter, "__init__", _boom)
    assert isinstance(get_segmenter(SPORTSBD), StubSegmenter)


def test_segmenter_base_is_abstract() -> None:
    with pytest.raises(TypeError):
        SegmenterBase()  # type: ignore[abstract]


# ── stage_segment integration: adapter routing + clip artifact contract ───────


class _FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_clip(self, video_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"video_id": video_id, **kwargs})
        return {"id": f"clip-{len(self.calls)}", **kwargs}


def _patch_stage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    segmenter: SegmenterBase,
    duration: float = 120.0,
) -> _FakeBackend:
    fake = _FakeBackend()
    monkeypatch.setattr(stage_segment.backend, "create_clip", fake.create_clip)
    monkeypatch.setattr(stage_segment, "_video_duration", lambda _p: duration)
    monkeypatch.setattr(
        stage_segment, "get_segmenter", lambda _v: segmenter
    )
    monkeypatch.setattr(
        stage_segment.r2,
        "download_to_temp",
        lambda _k: Path("/tmp/__segment_test_video.mp4"),
    )
    # Avoid the real .unlink(missing_ok=True) doing anything weird —
    # the path doesn't exist anyway, but make sure the test cleanup
    # never raises.
    return fake


def test_stage_segment_routes_through_adapter_and_writes_clips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seg = StubSegmenter(duration_s=120.0, period_s=30.0, confidence=0.8)
    fake = _patch_stage(monkeypatch, segmenter=seg, duration=120.0)

    result = stage_segment.run("vid-1", "r2://bucket/key/foo.mp4", "job-1")

    # 3 internal boundaries → 4 clips spanning [0, 30, 60, 90, 120].
    assert result["clip_count"] == 4
    assert len(result["clip_ids"]) == 4
    assert result["boundary_model"] == SOURCE_STUB

    # Clip artifact contract: each call uses boundary_source="model"
    # (preserved for backward compatibility with the clips API).
    for i, call in enumerate(fake.calls, start=1):
        assert call["video_id"] == "vid-1"
        assert call["boundary_source"] == "model"
        assert call["play_number"] == i
        assert call["job_id"] == "job-1"
        assert 0.0 <= call["boundary_confidence"] <= 1.0
    # Confidence is model-derived (not the historical flat 0.7).
    middle_confidences = [c["boundary_confidence"] for c in fake.calls[1:-1]]
    assert all(c == pytest.approx(0.8) for c in middle_confidences)


def test_stage_segment_no_boundaries_writes_single_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seg = StubSegmenter(no_boundaries=True)
    fake = _patch_stage(monkeypatch, segmenter=seg, duration=45.0)

    result = stage_segment.run("vid-2", "r2://b/k/x.mp4", "job-2")

    assert result["clip_count"] == 1
    assert len(fake.calls) == 1
    only = fake.calls[0]
    assert only["start_time"] == 0.0
    assert only["end_time"] == pytest.approx(45.0)
    assert only["boundary_source"] == "model"
    # No internal boundaries → segment is bounded by the start/end of
    # the video, which we treat as fully trusted.
    assert only["boundary_confidence"] == pytest.approx(1.0)


def test_stage_segment_drops_too_short_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Boundaries crammed within < MIN_PLAY_DURATION of each other and
    # of the start/end → all candidate segments collapse to one valid
    # clip (or zero, depending on placement).
    class _DenseSegmenter(SegmenterBase):
        source = "dense"

        def segment(self, video_path: Path) -> list[dict[str, Any]]:  # noqa: ARG002
            return [
                {"time_s": 0.5, "confidence": 0.9, "source": "dense"},
                {"time_s": 1.0, "confidence": 0.9, "source": "dense"},
                {"time_s": 1.4, "confidence": 0.9, "source": "dense"},
            ]

    fake = _patch_stage(monkeypatch, segmenter=_DenseSegmenter(), duration=60.0)
    result = stage_segment.run("vid-3", "r2://b/k/x.mp4", "job-3")
    # Only the trailing segment (1.4 → 60.0) clears MIN_PLAY_DURATION.
    assert result["clip_count"] == 1
    assert fake.calls[0]["start_time"] == pytest.approx(1.4)
    assert fake.calls[0]["end_time"] == pytest.approx(60.0)


def test_stage_segment_env_var_selects_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEGMENTER_VARIANT env var routes to the right adapter via the factory."""
    captured: dict[str, Any] = {}

    def fake_factory(variant: str) -> SegmenterBase:
        captured["variant"] = variant
        return StubSegmenter(no_boundaries=True)

    monkeypatch.setenv("SEGMENTER_VARIANT", LEARNED_PLAY)
    monkeypatch.setattr(stage_segment, "get_segmenter", fake_factory)
    monkeypatch.setattr(stage_segment.backend, "create_clip",
                        lambda *_a, **k: {"id": "x", **k})
    monkeypatch.setattr(stage_segment, "_video_duration", lambda _p: 60.0)
    monkeypatch.setattr(stage_segment.r2, "download_to_temp",
                        lambda _k: Path("/tmp/__seg_env.mp4"))

    stage_segment.run("vid-env", "r2://b/k/x.mp4", "job-env")
    assert captured["variant"] == LEARNED_PLAY
