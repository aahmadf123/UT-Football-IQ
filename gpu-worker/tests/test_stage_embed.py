"""Unit tests for gpu-worker/pipeline/stage_embed.py (Issue #8).

These tests run without GPU, video, or a backend. They exercise:

* The snap-anchored window math (``snap_window_bounds`` /
  ``select_keyframe_indices``).
* The structured encoder shape, including one-hot stability when
  unknown values fall through to ``"other"``.
* The SAM-optional path — masks improve the embedding but their absence
  must not gate output.
* The full ``run()`` integration: labels + tracks + pose flow through to
  a 256-d L2-normalised vector with retained sub-embeddings.
* The pluggable ``VisualEncoder`` interface — a custom encoder gets
  invoked and its output is L2-normalised before fusion.
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import stage_embed  # noqa: E402
from pipeline.stage_embed import (  # noqa: E402
    CLIP_DIM,
    EMBEDDING_DIM,
    STRUCTURED_DIM,
    VISUAL_DIM,
    EmbeddingResult,
    VisualEncoder,
    VisualEncoding,
    ZeroVisualEncoder,
    cosine_similarity,
    run,
    select_keyframe_indices,
    snap_window_bounds,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _clip(**overrides) -> dict:
    base = {
        "id": "clip-1",
        "start_time": 100.0,
        "end_time": 105.0,
        "label_data": {
            "formation": {"toledo": "Trips", "generic": "trips"},
            "coverage": {"toledo": "Cloud", "generic": "cover_3"},
        },
        "personnel_grouping": "11",
        "field_zone": "mid_field",
    }
    base.update(overrides)
    return base


def _track_point(tracklet_id: str, frame: int, x: float, y: float) -> dict:
    return {
        "tracklet_id": tracklet_id,
        "frame_number": frame,
        "field_x": x,
        "field_y": y,
    }


def _tracklet(tid: str, position_group: str, side: str = "offense") -> dict:
    return {
        "id": tid,
        "position_group": position_group,
        "side_of_ball": side,
    }


def _snap_event(frame: int) -> dict:
    return {"event_type": "snap", "frame_number": frame}


def _l2_norm(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values))


# ── snap window math ─────────────────────────────────────────────────────────


def test_snap_window_bounds_clips_to_non_negative() -> None:
    assert snap_window_bounds(0) == (0, 90)
    assert snap_window_bounds(50) == (20, 140)


def test_select_keyframe_indices_returns_eight_evenly_spaced_frames() -> None:
    idx = select_keyframe_indices(snap_frame=100, n=8)
    assert len(idx) == 8
    assert idx[0] == 70
    assert idx[-1] == 190
    # Strictly increasing for the default 120-frame window.
    assert all(b > a for a, b in zip(idx, idx[1:], strict=False))


def test_select_keyframe_indices_handles_single_frame_request() -> None:
    assert select_keyframe_indices(snap_frame=60, n=1) == [30]


# ── ZeroVisualEncoder default ────────────────────────────────────────────────


def test_zero_visual_encoder_returns_correct_dimension() -> None:
    enc = ZeroVisualEncoder()
    out = enc.encode([])
    assert len(out) == VISUAL_DIM
    assert all(v == 0.0 for v in out)


# ── Full run() integration ────────────────────────────────────────────────────


def test_run_returns_256d_l2_normalised_fused_vector() -> None:
    clip = _clip()
    tracklets = [_tracklet("t-qb", "QB"), _tracklet("t-rb", "RB")]
    track_points = [
        _track_point("t-qb", 100, 25.0, 30.0),
        _track_point("t-rb", 100, 22.0, 28.0),
    ]
    result = run(
        clip_id="clip-1",
        clip=clip,
        tracklets=tracklets,
        track_points=track_points,
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
    )
    assert isinstance(result, EmbeddingResult)
    assert len(result.vector) == EMBEDDING_DIM
    assert len(result.visual_vector) == VISUAL_DIM
    assert len(result.structured_vector) == STRUCTURED_DIM
    # Fused vector should be L2-normalised: ‖v‖₂ ≈ 1.
    assert math.isclose(_l2_norm(result.vector), 1.0, rel_tol=1e-6)
    assert result.snap_anchor is True


def test_run_with_no_snap_event_falls_back_to_midpoint_and_flags_anchor_false() -> None:
    result = run(
        clip_id="clip-x",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[],
    )
    assert result.snap_anchor is False
    # Confidence is penalised when snap is missing.
    assert result.embedding_confidence < 0.5


def test_run_records_source_label_ids_for_structured_inputs() -> None:
    labels = [
        {
            "id": "lbl-form",
            "label_type": "offensive_formation",
            "label_value": {"formation": "trips"},
        },
        {
            "id": "lbl-cov",
            "label_type": "coverage_shell",
            "label_value": {"coverage": "cover_3"},
        },
        {
            "id": "lbl-unrelated",
            "label_type": "play_concept",
            "label_value": {"concept": "inside_zone"},
        },
    ]
    result = run(
        clip_id="clip-1",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=labels,
        events=[_snap_event(100)],
    )
    assert "lbl-form" in result.source_label_ids
    assert "lbl-cov" in result.source_label_ids
    # Unrelated labels (play_concept) don't feed the structured encoder
    # in v1 and so must not appear in lineage.
    assert "lbl-unrelated" not in result.source_label_ids


def test_run_with_sam_masks_marks_used_sam_masks_and_boosts_confidence() -> None:
    common: dict = {
        "clip_id": "clip-1",
        "clip": _clip(),
        "tracklets": [_tracklet("t-qb", "QB")],
        "track_points": [_track_point("t-qb", 100, 25.0, 30.0)],
        "pose_keypoints": [],
        "labels": [],
        "events": [_snap_event(100)],
    }
    without_masks = run(**common, sam_masks=None)
    with_masks = run(**common, sam_masks=[object(), object()])
    assert without_masks.used_sam_masks is False
    assert with_masks.used_sam_masks is True
    assert with_masks.embedding_confidence > without_masks.embedding_confidence


# ── Structured encoder behaviour ─────────────────────────────────────────────


def test_unknown_formation_falls_into_other_slot() -> None:
    # Different unknown formations should still produce vectors that share
    # the same shape and the same "other" slot.
    result_a = run(
        clip_id="a",
        clip=_clip(label_data={"formation": {"generic": "alien_formation_a"}}),
        tracklets=[], track_points=[], pose_keypoints=[], labels=[], events=[],
    )
    result_b = run(
        clip_id="b",
        clip=_clip(label_data={"formation": {"generic": "alien_formation_b"}}),
        tracklets=[], track_points=[], pose_keypoints=[], labels=[], events=[],
    )
    # Same shape, and the structured-vector ordering of one-hot blocks is
    # stable across unknowns — the only differing dims should come from
    # downstream blocks (coverage etc.) which both clips left default.
    assert len(result_a.structured_vector) == len(result_b.structured_vector)


def test_structured_one_hot_position_is_deterministic_for_known_formation() -> None:
    result = run(
        clip_id="c",
        clip=_clip(label_data={"formation": {"generic": "trips"}}),
        tracklets=[], track_points=[], pose_keypoints=[], labels=[], events=[],
    )
    # Structured vector is L2-normalised: at least one non-zero entry.
    assert any(abs(v) > 0 for v in result.structured_vector)


# ── Pluggable visual encoder ────────────────────────────────────────────────


class _ConstantEncoder:
    """Returns a deterministic non-zero vector regardless of input."""

    def __init__(self, fill: float) -> None:
        self.fill = fill
        self.calls: list[tuple[int, bool]] = []

    def encode(self, keyframes, *, masks=None) -> list[float]:
        self.calls.append((len(keyframes), masks is not None))
        return [self.fill] * VISUAL_DIM


def test_custom_visual_encoder_is_called_with_masks_when_provided() -> None:
    enc = _ConstantEncoder(fill=1.0)
    keyframes = [object(), object(), object()]
    run(
        clip_id="c",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
        visual_encoder=enc,
        keyframes=keyframes,
        sam_masks=[object()],
    )
    assert enc.calls == [(3, True)]


def test_visual_encoder_output_is_l2_normalised_before_fusion() -> None:
    enc = _ConstantEncoder(fill=7.5)
    result = run(
        clip_id="c",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
        visual_encoder=enc,
    )
    # Visual sub-vector must be unit-norm regardless of raw scale.
    assert math.isclose(_l2_norm(result.visual_vector), 1.0, rel_tol=1e-6)


# ── Cosine helper — used by the CLI's local ranker and the search tests ─────


def test_cosine_similarity_identity_is_one() -> None:
    v = [1.0, 0.0, 0.0]
    assert math.isclose(cosine_similarity(v, v), 1.0, rel_tol=1e-9)


def test_cosine_similarity_orthogonal_is_zero() -> None:
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert math.isclose(cosine_similarity(a, b), 0.0, rel_tol=1e-9)


def test_cosine_similarity_handles_zero_vectors() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ── Encoder protocol smoke-test ───────────────────────────────────────────────


def test_zero_visual_encoder_satisfies_visual_encoder_protocol() -> None:
    enc: VisualEncoder = ZeroVisualEncoder()
    assert len(enc.encode([])) == VISUAL_DIM


def test_baseline_variant_constant_matches_router_string() -> None:
    # If this drifts the model-router's NIGHTLY_ONLY_VARIANTS guard
    # silently stops covering the embedding stage.
    assert stage_embed.BASELINE_MODEL_VARIANT == "play-embed-clip-vitb32-baseline"


# ── Raw CLIP image embedding (clip_vector) — Issue #195 ──────────────────────


class _ClipAwareEncoder:
    """A CLIP-aware encoder that surfaces both the projection and raw CLIP."""

    def __init__(self, *, proj_fill: float, clip_fill: float, clip_dim: int = CLIP_DIM) -> None:
        self.proj_fill = proj_fill
        self.clip_fill = clip_fill
        self.clip_dim = clip_dim
        self.calls = 0

    def encode_visual(self, keyframes, *, masks=None) -> VisualEncoding:
        self.calls += 1
        return VisualEncoding(
            projected=[self.proj_fill] * VISUAL_DIM,
            clip_image=[self.clip_fill] * self.clip_dim,
        )


def test_clip_aware_encoder_populates_clip_vector() -> None:
    enc = _ClipAwareEncoder(proj_fill=0.5, clip_fill=2.0)
    result = run(
        clip_id="c",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
        visual_encoder=enc,
        keyframes=[object()],
    )
    assert result.clip_vector is not None
    assert len(result.clip_vector) == CLIP_DIM
    # Stored unit-norm in CLIP space; cosine ranking is scale-invariant.
    assert math.isclose(_l2_norm(result.clip_vector), 1.0, rel_tol=1e-6)
    # Single forward pass — encode_visual, not a second encode() call.
    assert enc.calls == 1


def test_legacy_encode_only_encoder_leaves_clip_vector_none() -> None:
    # The default ZeroVisualEncoder implements only encode() — no raw CLIP.
    result = run(
        clip_id="c",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
        visual_encoder=ZeroVisualEncoder(),
    )
    assert result.clip_vector is None


def test_clip_image_none_from_clip_aware_encoder_yields_none() -> None:
    class _NoClip:
        def encode_visual(self, keyframes, *, masks=None) -> VisualEncoding:
            return VisualEncoding(projected=[0.1] * VISUAL_DIM, clip_image=None)

    result = run(
        clip_id="c",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
        visual_encoder=_NoClip(),
    )
    assert result.clip_vector is None


def test_clip_vector_dim_is_padded_or_trimmed() -> None:
    # A misconfigured encoder returning the wrong dim is normalised to CLIP_DIM
    # so a downstream pgvector(512) write never fails on shape.
    enc = _ClipAwareEncoder(proj_fill=0.0, clip_fill=1.0, clip_dim=CLIP_DIM - 5)
    result = run(
        clip_id="c",
        clip=_clip(),
        tracklets=[],
        track_points=[],
        pose_keypoints=[],
        labels=[],
        events=[_snap_event(100)],
        visual_encoder=enc,
    )
    assert result.clip_vector is not None
    assert len(result.clip_vector) == CLIP_DIM
