"""Stage — learned play embeddings (Issue #8, design in #77).

Produces a single 256-d ``play`` embedding per clip from:

* The snap-anchored window over ``track_points`` (player XY in field yards).
* Snap-frame ``pose_keypoints`` features (head yaw per DB, stance per OL).
* Clip-scope ``labels`` (formation / coverage / personnel one-hots).
* Optional SAM masks — when present they are passed to the visual encoder
  as a background-suppression hint; absence does not gate embedding output.

The fused vector is built as::

    visual_vec_192 = L2_norm( visual_encoder(keyframes, mask=optional) )
    structured_vec_64 = L2_norm( structured_encoder(labels, tracks, pose) )
    play_vec_256 = L2_norm( concat( visual_vec_192, structured_vec_64 ) )

The visual encoder is pluggable. ``ZeroVisualEncoder`` (the default)
returns a zero vector so the stage runs without GPU or model weights,
which keeps the same-session/nightly routing decision orthogonal to
encoder availability. Production deploys swap in
``ClipViTB32VisualEncoder`` once the weights are mounted on the worker.

This stage is **nightly-only** per ``docs/embeddings-architecture.md``
§11 and the corresponding ``NIGHTLY_ONLY_VARIANTS`` entry in
``pipeline/model_router.py``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)

# ── Dimensions — must match backend.app.models.PLAY_EMBEDDING_* ──────────────
EMBEDDING_DIM: int = 256
VISUAL_DIM: int = 192
STRUCTURED_DIM: int = 64
assert VISUAL_DIM + STRUCTURED_DIM == EMBEDDING_DIM
# Raw CLIP ViT-B/32 image embedding dimension (Issue #195). This is the
# *unprojected* visual encoder output, in CLIP's shared text-image space —
# retained so a CLIP text-tower query can be cosine-compared against it.
# Must match ``backend.app.models.PLAY_EMBEDDING_CLIP_DIM``.
CLIP_DIM: int = 512

# ── Baseline model identifier (also registered in model_router as nightly-only)
BASELINE_MODEL_VARIANT: str = "play-embed-clip-vitb32-baseline"

# ── Frozen one-hot vocabularies for the structured encoder ────────────────────
# Order matters — the structured vector is treated as a fixed-shape feature
# array. New values land in the trailing ``"other"`` slot until the
# vocabulary is bumped and the model_version is rotated.
FORMATION_VOCAB: tuple[str, ...] = (
    "2x2",
    "3x1",
    "2x1",
    "empty",
    "condensed",
    "trips",
    "doubles",
    "ace",
    "pistol",
    "i_form",
    "gun",
    "wildcat",
    "other",
)
COVERAGE_VOCAB: tuple[str, ...] = (
    "cover_0",
    "cover_1",
    "cover_2",
    "cover_3",
    "cover_4",
    "cover_6",
    "match",
    "buzz",
    "cloud",
    "other",
)
PERSONNEL_VOCAB: tuple[str, ...] = (
    "10",
    "11",
    "12",
    "13",
    "20",
    "21",
    "22",
    "other",
)
FIELD_ZONE_VOCAB: tuple[str, ...] = (
    "own_red_zone",
    "own_territory",
    "mid_field",
    "opp_territory",
    "opp_red_zone",
    "other",
)
HASH_VOCAB: tuple[str, ...] = ("left", "middle", "right", "other")

# Position groups whose snap-frame backfield XY we encode as a relative offset.
BACKFIELD_POSITIONS: tuple[str, ...] = ("QB", "RB", "SLOT", "X", "Z")

# Inputs to the snap-anchored window, see design doc §5.1.
SNAP_WINDOW_FRAMES_PRE: int = 30
SNAP_WINDOW_FRAMES_POST: int = 90
SNAP_WINDOW_KEYFRAMES: int = 8


# ── Result shape ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbeddingResult:
    """Outputs ready to write to ``playembeddings``."""

    vector: list[float]
    visual_vector: list[float]
    structured_vector: list[float]
    snap_anchor: bool
    embedding_confidence: float
    used_sam_masks: bool
    source_label_ids: list[str] = field(default_factory=list)
    chunk_kind: str = "play"
    # Raw 512-d CLIP image embedding in CLIP shared space (Issue #195), or
    # ``None`` when the visual encoder cannot surface one (e.g. the default
    # ``ZeroVisualEncoder``). When present it is persisted to
    # ``playembeddings.clip_vector`` and powers ``/api/v1/search/text``.
    clip_vector: list[float] | None = None


# ── Visual encoder interface ─────────────────────────────────────────────────


class VisualEncoder(Protocol):
    """Encodes a stack of snap-anchored keyframes into a fixed-dim vector.

    Implementations must return exactly ``VISUAL_DIM`` floats; the stage
    L2-normalises the output before fusion so encoders do not need to do
    it themselves.
    """

    def encode(
        self, keyframes: list[Any], *, masks: list[Any] | None = None
    ) -> list[float]: ...


@dataclass(frozen=True)
class VisualEncoding:
    """Combined output of a CLIP-aware visual encoder (Issue #195).

    ``projected`` is the ``VISUAL_DIM`` (192-d) vector that fuses into the
    play embedding — the existing behaviour. ``clip_image`` is the raw
    ``CLIP_DIM`` (512-d) image embedding *before* projection, in CLIP's
    shared text-image space, retained for CLIP text-tower search. Both are
    L2-normalised by the stage, so encoders need not normalise themselves.
    """

    projected: list[float]
    clip_image: list[float] | None = None


@runtime_checkable
class ClipAwareVisualEncoder(Protocol):
    """A visual encoder that can also surface the raw CLIP image embedding.

    Production's CLIP ViT-B/32 encoder computes the 512-d image embedding
    and *then* projects it to ``VISUAL_DIM``; implementing ``encode_visual``
    lets it return both from a single forward pass (no double compute) so
    ``stage_embed`` can persist the raw embedding for ``/search/text``.
    Encoders that only implement ``encode`` keep working unchanged — their
    ``clip_vector`` is simply ``None``.
    """

    def encode_visual(
        self, keyframes: list[Any], *, masks: list[Any] | None = None
    ) -> VisualEncoding: ...


class ZeroVisualEncoder:
    """No-op visual encoder used when CLIP weights are unavailable.

    Returns a ``VISUAL_DIM``-zero vector and surfaces **no** raw CLIP
    embedding (it has no CLIP weights to run), so the resulting row's
    ``clip_vector`` stays ``NULL`` and is invisible to text search. The
    fused play embedding then behaves like the structured-only
    sub-embedding (renormalised) — this is intentional for v1 so the rest
    of the retrieval surface (pgvector index, search router, CLI) can be
    exercised end-to-end without GPU prerequisites.
    """

    def encode(
        self, keyframes: list[Any], *, masks: list[Any] | None = None
    ) -> list[float]:
        del keyframes, masks
        return [0.0] * VISUAL_DIM


def _encode_visual(
    encoder: VisualEncoder,
    keyframes: list[Any],
    masks: list[Any] | None,
) -> tuple[list[float], list[float] | None]:
    """Resolve ``(projected_192, raw_clip_512_or_none)`` from any encoder.

    Prefers a CLIP-aware encoder's single-pass ``encode_visual`` (so the
    512-d CLIP embedding is captured without recomputing it); falls back to
    the legacy ``encode`` interface (raw CLIP embedding unavailable → None).
    """
    if isinstance(encoder, ClipAwareVisualEncoder):
        enc = encoder.encode_visual(keyframes, masks=masks)
        return list(enc.projected), (None if enc.clip_image is None else list(enc.clip_image))
    return list(encoder.encode(keyframes, masks=masks)), None


# ── Public entry point ────────────────────────────────────────────────────────


def run(
    clip_id: str,
    clip: dict[str, Any],
    tracklets: list[dict[str, Any]],
    track_points: list[dict[str, Any]],
    pose_keypoints: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    fps: float = 60.0,
    visual_encoder: VisualEncoder | None = None,
    keyframes: list[Any] | None = None,
    sam_masks: list[Any] | None = None,
) -> EmbeddingResult:
    """Compute the play embedding for a single clip.

    Args:
        clip_id: Source clip UUID (kept for log lineage only).
        clip: The ``clips`` row as a dict (uses ``label_data``,
            ``personnel_grouping``, ``field_zone``, ``start_time`` /
            ``end_time``).
        tracklets: Tracklet dicts; ``position_group`` / ``side_of_ball``
            drive the structured geometry features.
        track_points: All ``track_points`` rows for the clip.  The stage
            filters to the snap-anchored window internally.
        pose_keypoints: ``pose_keypoints`` rows for the snap frame
            (others are ignored by the structured encoder for v1).
        labels: ``labels`` rows scoped to the clip; supplies formation,
            coverage and personnel one-hots.
        events: Used to locate the snap frame (``event_type == 'snap'``).
        fps: Source FPS — used to clip the snap-anchored window when
            ``frame_number`` is missing on track points.
        visual_encoder: Plug-in encoder; defaults to ``ZeroVisualEncoder``
            so the stage runs without CLIP weights.
        keyframes: Optional pre-extracted keyframes for the visual
            encoder.  When ``None`` the encoder is still called with an
            empty list — ``ZeroVisualEncoder`` is happy with that.
        sam_masks: Optional SAM 3.1 masks; passed through to the visual
            encoder and recorded in ``used_sam_masks``.

    Returns:
        A populated ``EmbeddingResult``.
    """
    log.info(
        "stage_embed_start",
        clip_id=clip_id,
        tracklet_count=len(tracklets),
        track_point_count=len(track_points),
        pose_kp_count=len(pose_keypoints),
        label_count=len(labels),
        has_sam_masks=bool(sam_masks),
    )

    snap_frame, snap_anchor = _resolve_snap_frame(events, clip, fps)

    structured_vec_raw, source_label_ids = _structured_features(
        clip=clip,
        labels=labels,
        tracklets=tracklets,
        track_points=track_points,
        pose_keypoints=pose_keypoints,
        snap_frame=snap_frame,
    )
    structured_vec = _l2_normalise(_pad_or_trim(structured_vec_raw, STRUCTURED_DIM))

    encoder: VisualEncoder = visual_encoder or ZeroVisualEncoder()
    raw_visual, raw_clip = _encode_visual(encoder, keyframes or [], sam_masks)
    visual_vec = _l2_normalise(_pad_or_trim(list(raw_visual), VISUAL_DIM))
    # Retain the raw CLIP image embedding (CLIP shared space) when the
    # encoder produced one — it powers genuine CLIP text-tower search
    # (Issue #195). L2-normalised for unit-norm storage; cosine ranking is
    # scale-invariant either way.
    clip_vec: list[float] | None = None
    if raw_clip is not None:
        clip_vec = _l2_normalise(_pad_or_trim(list(raw_clip), CLIP_DIM))

    fused = _l2_normalise(list(visual_vec) + list(structured_vec))

    confidence = _embedding_confidence(
        snap_anchor=snap_anchor,
        tracklet_count=len(tracklets),
        pose_kp_count=len(pose_keypoints),
        label_count=len(labels),
        used_sam_masks=sam_masks is not None and len(sam_masks) > 0,
    )

    result = EmbeddingResult(
        vector=fused,
        visual_vector=list(visual_vec),
        structured_vector=list(structured_vec),
        snap_anchor=snap_anchor,
        embedding_confidence=confidence,
        used_sam_masks=bool(sam_masks),
        source_label_ids=source_label_ids,
        chunk_kind="play",
        clip_vector=clip_vec,
    )

    log.info(
        "stage_embed_done",
        clip_id=clip_id,
        snap_anchor=snap_anchor,
        embedding_confidence=confidence,
        used_sam_masks=result.used_sam_masks,
        source_label_count=len(source_label_ids),
        has_clip_vector=clip_vec is not None,
    )
    return result


# ── Snap-anchored window ──────────────────────────────────────────────────────


def _resolve_snap_frame(
    events: list[dict[str, Any]], clip: dict[str, Any], fps: float
) -> tuple[int, bool]:
    """Locate the snap frame for the clip; fall back to clip midpoint."""
    for ev in events:
        if (ev.get("event_type") or "").lower() != "snap":
            continue
        frame = ev.get("frame_number")
        if isinstance(frame, int):
            return frame, True
        ts = ev.get("timestamp_seconds")
        if isinstance(ts, (int, float)):
            start = float(clip.get("start_time", 0.0))
            return int(round((float(ts) - start) * fps)), True
    # Fallback: midpoint of the clip in frames. ``snap_anchor=False`` is
    # surfaced on the embedding row so downstream consumers know the chunk
    # was not aligned to an actual snap event.
    start = float(clip.get("start_time", 0.0))
    end = float(clip.get("end_time", start + 1.0))
    mid_seconds = (start + end) / 2.0 - start
    return max(int(round(mid_seconds * fps)), 0), False


def snap_window_bounds(snap_frame: int) -> tuple[int, int]:
    """Return ``[snap-30, snap+90]`` clipped to non-negative frame indices."""
    return (max(snap_frame - SNAP_WINDOW_FRAMES_PRE, 0), snap_frame + SNAP_WINDOW_FRAMES_POST)


def select_keyframe_indices(snap_frame: int, n: int = SNAP_WINDOW_KEYFRAMES) -> list[int]:
    """Uniformly subsample ``n`` keyframe indices inside the snap window."""
    start, end = snap_window_bounds(snap_frame)
    if end <= start or n <= 0:
        return []
    if n == 1:
        return [start]
    step = (end - start) / (n - 1)
    return [int(round(start + i * step)) for i in range(n)]


# ── Structured encoder ────────────────────────────────────────────────────────


def _structured_features(
    *,
    clip: dict[str, Any],
    labels: list[dict[str, Any]],
    tracklets: list[dict[str, Any]],
    track_points: list[dict[str, Any]],
    pose_keypoints: list[dict[str, Any]],
    snap_frame: int,
) -> tuple[list[float], list[str]]:
    """Build the raw structured feature vector + the labels that fed it."""
    label_data = dict(clip.get("label_data") or {})

    formation = _read_generic(label_data.get("formation")) or _label_value(
        labels, "offensive_formation", "formation"
    )
    coverage = _read_generic(label_data.get("coverage")) or _label_value(
        labels, "coverage_shell", "coverage"
    )
    personnel = (
        clip.get("personnel_grouping")
        or _label_value(labels, "personnel", "grouping")
    )
    field_zone = clip.get("field_zone")
    hash_mark = _read_generic(label_data.get("hash")) or _label_value(
        labels, "hash_mark", "hash"
    )

    parts: list[float] = []
    parts.extend(_one_hot(formation, FORMATION_VOCAB))
    parts.extend(_one_hot(coverage, COVERAGE_VOCAB))
    parts.extend(_one_hot(personnel, PERSONNEL_VOCAB))
    parts.extend(_one_hot(field_zone, FIELD_ZONE_VOCAB))
    parts.extend(_one_hot(hash_mark, HASH_VOCAB))

    parts.extend(_backfield_geometry(tracklets, track_points, snap_frame))
    parts.extend(_defensive_box_geometry(tracklets, track_points, snap_frame))
    parts.extend(_pose_summary(tracklets, pose_keypoints, snap_frame))

    source_label_ids: list[str] = []
    for lbl in labels:
        lbl_id = lbl.get("id")
        if lbl_id is None:
            continue
        if lbl.get("label_type") in {
            "offensive_formation",
            "coverage_shell",
            "personnel",
            "hash_mark",
        }:
            source_label_ids.append(str(lbl_id))

    return parts, source_label_ids


def _one_hot(value: str | None, vocab: tuple[str, ...]) -> list[float]:
    """Return a fixed-length one-hot vector for ``value`` over ``vocab``.

    Unknown values land in the trailing ``"other"`` slot when present, so
    the structured vector shape is stable across vocabulary drift.
    """
    out = [0.0] * len(vocab)
    if not value:
        return out
    norm = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    for i, candidate in enumerate(vocab):
        if candidate == norm:
            out[i] = 1.0
            return out
    if vocab and vocab[-1] == "other":
        out[-1] = 1.0
    return out


def _read_generic(payload: Any) -> str | None:
    """Extract the ``generic`` slot from a label payload of the
    ``{toledo, generic, confidence}`` shape used by ``clips.label_data``."""
    if isinstance(payload, dict):
        for key in ("generic", "value", "label"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    elif isinstance(payload, str):
        return payload
    return None


def _label_value(
    labels: list[dict[str, Any]], label_type: str, key: str
) -> str | None:
    for lbl in labels:
        if lbl.get("label_type") != label_type:
            continue
        value = (lbl.get("label_value") or {}).get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _snap_track_points(
    track_points: list[dict[str, Any]], snap_frame: int
) -> list[dict[str, Any]]:
    """Return track_points within ±2 frames of the snap (or all if frame missing)."""
    nearest: list[dict[str, Any]] = []
    for tp in track_points:
        frame = tp.get("frame_number")
        if frame is None or abs(int(frame) - snap_frame) <= 2:
            nearest.append(tp)
    return nearest


def _tracklet_at_snap(
    tracklet: dict[str, Any],
    track_points: list[dict[str, Any]],
    snap_frame: int,
) -> tuple[float, float] | None:
    """XY position (yards) for a tracklet at — or nearest to — the snap."""
    tid = tracklet.get("id")
    best: tuple[int, float, float] | None = None
    for tp in track_points:
        if tp.get("tracklet_id") != tid:
            continue
        x = tp.get("field_x")
        y = tp.get("field_y")
        if x is None or y is None:
            continue
        frame = tp.get("frame_number")
        if frame is None:
            return float(x), float(y)
        distance = abs(int(frame) - snap_frame)
        if best is None or distance < best[0]:
            best = (distance, float(x), float(y))
    return (best[1], best[2]) if best else None


def _backfield_geometry(
    tracklets: list[dict[str, Any]],
    track_points: list[dict[str, Any]],
    snap_frame: int,
) -> list[float]:
    """Per-position-group (x, y) yards relative to the QB at snap.

    Output is a fixed-length 2 * len(BACKFIELD_POSITIONS) vector. Missing
    positions get zeros; the QB itself contributes ``(0, 0)`` so the rest
    of the vector is centred even when only the QB is tracked.
    """
    snap_positions: dict[str, tuple[float, float]] = {}
    for t in tracklets:
        pg = (t.get("position_group") or "").upper()
        if pg not in BACKFIELD_POSITIONS:
            continue
        xy = _tracklet_at_snap(t, track_points, snap_frame)
        if xy is not None:
            snap_positions.setdefault(pg, xy)

    qb_xy = snap_positions.get("QB", (0.0, 0.0))
    out: list[float] = []
    for pg in BACKFIELD_POSITIONS:
        if pg in snap_positions:
            x, y = snap_positions[pg]
            out.extend([x - qb_xy[0], y - qb_xy[1]])
        else:
            out.extend([0.0, 0.0])
    return out


def _defensive_box_geometry(
    tracklets: list[dict[str, Any]],
    track_points: list[dict[str, Any]],
    snap_frame: int,
) -> list[float]:
    """Defenders inside 5 yards of the line of scrimmage.

    For v1 we approximate the LOS by the snap-frame mean ``field_y`` of
    all *offensive* tracklets — Toledo's calibration stack already
    projects to yards on the field, so the y-axis is the LOS-aligned
    axis.
    """
    snap_points = _snap_track_points(track_points, snap_frame)
    offensive_ys = []
    for t in tracklets:
        if (t.get("side_of_ball") or "").lower() != "offense":
            continue
        xy = _tracklet_at_snap(t, snap_points, snap_frame)
        if xy is not None:
            offensive_ys.append(xy[1])
    if not offensive_ys:
        return [0.0, 0.0, 0.0]
    los_y = statistics.fmean(offensive_ys)

    box_xs: list[float] = []
    box_ys: list[float] = []
    for t in tracklets:
        if (t.get("side_of_ball") or "").lower() != "defense":
            continue
        xy = _tracklet_at_snap(t, snap_points, snap_frame)
        if xy is None:
            continue
        if abs(xy[1] - los_y) <= 5.0:
            box_xs.append(xy[0])
            box_ys.append(xy[1] - los_y)

    if not box_xs:
        return [0.0, 0.0, 0.0]
    return [
        float(len(box_xs)),
        statistics.fmean(box_xs),
        statistics.fmean(box_ys),
    ]


def _pose_summary(
    tracklets: list[dict[str, Any]],
    pose_keypoints: list[dict[str, Any]],
    snap_frame: int,
) -> list[float]:
    """Mean DB head-yaw + mean OL stance angle at snap."""
    tracklet_by_id: dict[Any, dict[str, Any]] = {
        t.get("id"): t for t in tracklets if t.get("id") is not None
    }

    db_yaws: list[float] = []
    ol_torsos: list[float] = []
    for kp in pose_keypoints:
        if kp.get("frame_number") != snap_frame:
            continue
        tid = kp.get("tracklet_id")
        tr = tracklet_by_id.get(tid)
        if tr is None:
            continue
        pg = (tr.get("position_group") or "").upper()
        bio = kp.get("biomechanics") or {}
        if pg in ("CB", "FS", "SS", "S", "DB", "NB"):
            yaw = kp.get("head_yaw_degrees")
            if isinstance(yaw, (int, float)):
                db_yaws.append(float(yaw))
        if pg in ("OL", "OT", "OG", "OC"):
            torso = bio.get("torso_angle_degrees")
            if isinstance(torso, (int, float)):
                ol_torsos.append(float(torso))

    return [
        statistics.fmean(db_yaws) if db_yaws else 0.0,
        statistics.fmean(ol_torsos) if ol_torsos else 0.0,
    ]


# ── Confidence & math helpers ────────────────────────────────────────────────


def _embedding_confidence(
    *,
    snap_anchor: bool,
    tracklet_count: int,
    pose_kp_count: int,
    label_count: int,
    used_sam_masks: bool,
) -> float:
    """Combine evidence-availability signals into a 0–1 confidence score.

    Coarse and deliberately interpretable — coaches see this directly in
    the search response envelope and we want the components to be
    auditable, not opaque.
    """
    score = 0.4 if snap_anchor else 0.1
    score += min(tracklet_count / 22.0, 1.0) * 0.2
    score += min(pose_kp_count / 11.0, 1.0) * 0.15
    score += min(label_count / 4.0, 1.0) * 0.15
    if used_sam_masks:
        score += 0.1
    return round(min(score, 1.0), 4)


def _pad_or_trim(values: list[float], target_dim: int) -> list[float]:
    if len(values) == target_dim:
        return list(values)
    if len(values) > target_dim:
        return values[:target_dim]
    return list(values) + [0.0] * (target_dim - len(values))


def _l2_normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return list(values)
    return [v / norm for v in values]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Helper used in tests and the local CLI ranker for nearest neighbours."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
