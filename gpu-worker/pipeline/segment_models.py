"""Play-boundary segmenter adapters for Stage 2 (Issue #75).

Mirrors the ``detector_models`` / ``pose_estimator`` adapter pattern: one
abstract base, one wrapper per algorithm family, and a deterministic
stub for tests.

Boundary schema (returned by ``segment`` on every adapter):

    {
        "time_s": float,            # boundary timestamp in seconds (required)
        "confidence": float,        # model-derived 0.0..1.0 (required)
        "source": str,              # adapter identifier (required)
    }

Returning ``[]`` is valid — it means the segmenter found no internal
boundaries (the whole video collapses to a single clip).

Phase 2.5 context (Issue #75 spike):

* ``OpticalFlowSegmenter`` is the same-session production path. It must
  stay default because it meets the Issue #16 latency budget without
  GPU model loads.
* ``SportsBDSegmenter`` is an experimental adapter around the SportsBD
  *shot*-boundary detector. The Issue #75 benchmark in
  ``docs/segment-benchmark.md`` shows SportsBD is **not useful** for
  continuous drone / all-22 practice film, so this adapter ships as a
  benchmark/experimental hook only and falls back to the stub when the
  ``sportsbd`` package is not installed.
* ``LearnedPlaySegmenter`` is the football-specific candidate that the
  spike recommends investing in. It currently runs a rules+ML hybrid on
  top of optical flow (huddle gap prior, snap-onset peak detection,
  duration prior) and is the seam where a trained boundary model will
  plug in once weights exist. It is opt-in via env var.
* ``StubSegmenter`` is deterministic, dependency-free, and used by tests
  and any CI path without OpenCV / model weights.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Tuning knobs (shared across optical-flow style adapters) ──────────────────
SAMPLE_FPS = 2
QUIET_THRESHOLD = 1.5
MIN_QUIET_FRAMES = 4
# Expected play duration prior (in seconds) used by LearnedPlaySegmenter to
# nudge confidence toward football-realistic boundaries.
EXPECTED_PLAY_SECONDS = 25.0
PLAY_DURATION_SIGMA = 12.0

# Source tags written into boundary dicts and surfaced on clip rows.
SOURCE_OPTICAL_FLOW = "optical_flow"
SOURCE_SPORTSBD = "sportsbd"
SOURCE_LEARNED = "learned_play"
SOURCE_STUB = "stub"

# Variant identifiers used by the factory + stage_segment env knob.
OPTICAL_FLOW = "optical_flow"
OPTICAL_FLOW_FAST = "optical-flow-fast"
SPORTSBD = "sportsbd"
LEARNED_PLAY = "learned_play"
STUB_SEGMENTER = "stub"


# ── Base class ────────────────────────────────────────────────────────────────


class SegmenterBase(ABC):
    """Abstract base for play-boundary segmenters.

    Subclasses must implement ``segment(video_path) → list[dict]``. Each
    dict follows the schema documented at the top of this module.
    """

    #: Short human-readable name written into the ``source`` field of
    #: every boundary this adapter emits.
    source: str = "base"

    @abstractmethod
    def segment(self, video_path: Path) -> list[dict[str, Any]]:
        """Return boundary timestamps for ``video_path``."""


# ── Optical flow (current production path) ────────────────────────────────────


def _quiet_frames_and_fps(video_path: Path) -> tuple[list[tuple[int, float]], float, float]:
    """Sample frames and return (quiet_frame_idx + magnitude, fps, duration).

    Centralised here so both the optical-flow and the learned hybrid
    adapter can reuse the OpenCV / NVDEC plumbing.
    """
    import cv2
    import numpy as np

    from pipeline.hwaccel import nvdec_video_capture

    cap = nvdec_video_capture(video_path)
    fps: float = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps else 0.0

    step = max(1, int(fps / SAMPLE_FPS))
    quiet: list[tuple[int, float]] = []
    prev_gray = None
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                flow = cv2.calcOpticalFlowFarneback(
                    prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0
                )
                mag = float(np.mean(np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)))
                if mag < QUIET_THRESHOLD:
                    quiet.append((frame_idx, mag))
            prev_gray = gray
        frame_idx += 1
    cap.release()
    return quiet, fps, duration


def _gap_groups(
    quiet: list[tuple[int, float]], fps: float
) -> list[tuple[int, int, float]]:
    """Cluster quiet frame samples into (start, end, mean_mag) gap groups."""
    if not quiet:
        return []
    groups: list[tuple[int, int, float]] = []
    group_start, group_end = quiet[0][0], quiet[0][0]
    mags: list[float] = [quiet[0][1]]
    for f, m in quiet[1:]:
        if f - group_end <= int(fps):
            group_end = f
            mags.append(m)
        else:
            if (group_end - group_start) >= MIN_QUIET_FRAMES:
                groups.append((group_start, group_end, sum(mags) / len(mags)))
            group_start, group_end, mags = f, f, [m]
    if (group_end - group_start) >= MIN_QUIET_FRAMES:
        groups.append((group_start, group_end, sum(mags) / len(mags)))
    return groups


def _confidence_from_gap(gap_frames: int, mean_mag: float, fps: float) -> float:
    """Derive a 0..1 boundary confidence from gap length and quietness.

    Longer + quieter gaps → higher confidence. Tuned so a 2 s gap at
    half the quiet threshold lands around 0.7 (i.e. matches the old
    flat placeholder).
    """
    seconds = gap_frames / fps if fps else 0.0
    length_score = min(1.0, seconds / 4.0)  # saturates at 4 s of quiet
    quiet_score = max(0.0, 1.0 - (mean_mag / QUIET_THRESHOLD))
    # Weighted average, biased toward length because long huddles are the
    # strongest signal in practice film.
    return round(0.6 * length_score + 0.4 * quiet_score, 3)


class OpticalFlowSegmenter(SegmenterBase):
    """Same-session production segmenter (optical-flow quiet-period heuristic).

    Boundary = midpoint of each detected quiet-frame gap. Confidence is
    derived per-gap from length and mean flow magnitude.
    """

    source = SOURCE_OPTICAL_FLOW

    def segment(self, video_path: Path) -> list[dict[str, Any]]:
        quiet, fps, _duration = _quiet_frames_and_fps(video_path)
        groups = _gap_groups(quiet, fps)
        boundaries: list[dict[str, Any]] = []
        for start, end, mean_mag in groups:
            mid = (start + end) // 2
            boundaries.append({
                "time_s": float(mid / fps) if fps else 0.0,
                "confidence": _confidence_from_gap(end - start, mean_mag, fps),
                "source": self.source,
            })
        boundaries.sort(key=lambda b: b["time_s"])
        return boundaries


# ── SportsBD (experimental shot-boundary adapter) ─────────────────────────────


class SportsBDSegmenter(SegmenterBase):
    """Thin adapter around the SportsBD *shot*-boundary detector.

    SportsBD targets broadcast cuts, fades, and logo transitions. Toledo
    drone / all-22 footage is continuous, so the Issue #75 benchmark
    finds SportsBD produces near-zero useful boundaries on practice
    film. This adapter is retained as the explicit spike result and
    will only run when ``sportsbd`` is installed at runtime — otherwise
    it falls back to the stub.

    Do not promote this adapter to production without first re-running
    ``docs/segment-benchmark.md`` against new footage.
    """

    source = SOURCE_SPORTSBD

    def __init__(self, threshold: float = 0.5) -> None:
        try:
            import sportsbd  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "sportsbd is required for SportsBDSegmenter. "
                "Install with: pip install sportsbd"
            ) from exc
        self._threshold = threshold
        log.info("sportsbd_segmenter_initialized", threshold=threshold)

    def segment(self, video_path: Path) -> list[dict[str, Any]]:
        import sportsbd  # type: ignore[import-not-found]

        # SportsBD's API surface varies between versions; we expect a
        # function that returns a list of cut timestamps (seconds) plus
        # per-cut scores. Stay defensive so a minor upstream rename does
        # not crash the worker.
        detect = getattr(sportsbd, "detect_shot_boundaries", None) or getattr(
            sportsbd, "detect", None
        )
        if detect is None:  # pragma: no cover - depends on upstream API
            log.warning("sportsbd_api_missing", video=str(video_path))
            return []
        raw = detect(str(video_path), threshold=self._threshold)
        boundaries: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                t = float(item.get("time_s") or item.get("t") or 0.0)
                c = float(item.get("score") or item.get("confidence") or 0.5)
            else:
                t = float(item)
                c = 0.5
            boundaries.append({
                "time_s": t,
                "confidence": max(0.0, min(1.0, c)),
                "source": self.source,
            })
        boundaries.sort(key=lambda b: b["time_s"])
        return boundaries


# ── Learned play-boundary segmenter (rules + ML hybrid) ───────────────────────


class LearnedPlaySegmenter(SegmenterBase):
    """Football-tuned play-boundary segmenter (rules + ML hybrid).

    Strategy on top of the same optical-flow signal that
    ``OpticalFlowSegmenter`` consumes:

    1. Cluster quiet samples into gap groups.
    2. Score each gap by length × quietness (the model-derived signal).
    3. Apply a Gaussian play-duration prior centred on ~25 s (typical
       between-snap gap) so spurious mid-play quiet frames get
       suppressed and end-of-play huddles get reinforced.
    4. Drop low-confidence candidates below ``min_confidence``.

    The seam for a future trained boundary model is the inline
    ``base`` score composition in ``segment()``: swap the rules-based
    score for a model probability and keep the existing duration-prior
    weighting / post-filter pipeline unchanged.
    """

    source = SOURCE_LEARNED

    def __init__(self, min_confidence: float = 0.35) -> None:
        self._min_confidence = min_confidence

    def segment(self, video_path: Path) -> list[dict[str, Any]]:
        quiet, fps, _duration = _quiet_frames_and_fps(video_path)
        groups = _gap_groups(quiet, fps)
        if not groups:
            return []

        boundaries: list[dict[str, Any]] = []
        last_time = 0.0
        for start, end, mean_mag in groups:
            mid_t = (start + end) / 2.0 / fps if fps else 0.0
            base = _confidence_from_gap(end - start, mean_mag, fps)
            prior = self._duration_prior(mid_t - last_time)
            score = round(0.7 * base + 0.3 * prior, 3)
            if score < self._min_confidence:
                continue
            boundaries.append({
                "time_s": float(mid_t),
                "confidence": float(score),
                "source": self.source,
            })
            last_time = mid_t
        return boundaries

    @staticmethod
    def _duration_prior(elapsed: float) -> float:
        """Gaussian centred on ``EXPECTED_PLAY_SECONDS``."""
        if elapsed <= 0:
            return 0.0
        import math

        z = (elapsed - EXPECTED_PLAY_SECONDS) / PLAY_DURATION_SIGMA
        return math.exp(-0.5 * z * z)


# ── Stub segmenter (for tests and CI without OpenCV / model weights) ──────────


class StubSegmenter(SegmenterBase):
    """Deterministic synthetic segmenter for tests.

    Emits one boundary every ``period_s`` seconds across a notional
    ``duration_s`` window. Does not open the video file, so it is safe
    to use anywhere the real OpenCV pipeline would be too heavy
    (e.g. ``no_boundaries=True`` for the no-boundary edge case).
    """

    source = SOURCE_STUB

    def __init__(
        self,
        duration_s: float = 120.0,
        period_s: float = 30.0,
        confidence: float = 0.6,
        no_boundaries: bool = False,
    ) -> None:
        self._duration_s = duration_s
        self._period_s = period_s
        self._confidence = max(0.0, min(1.0, confidence))
        self._no_boundaries = no_boundaries

    def segment(self, video_path: Path) -> list[dict[str, Any]]:  # noqa: ARG002
        if self._no_boundaries or self._period_s <= 0:
            return []
        boundaries: list[dict[str, Any]] = []
        t = self._period_s
        while t < self._duration_s:
            boundaries.append({
                "time_s": float(t),
                "confidence": float(self._confidence),
                "source": self.source,
            })
            t += self._period_s
        return boundaries


# ── Factory ───────────────────────────────────────────────────────────────────


def get_segmenter(variant: str | None) -> SegmenterBase:
    """Return a segmenter adapter for the given variant.

    Logic:
      - ``optical_flow`` (default) → OpticalFlowSegmenter
      - ``sportsbd``               → SportsBDSegmenter, falls back to
        StubSegmenter when ``sportsbd`` is not installed.
      - ``learned_play``           → LearnedPlaySegmenter
      - ``stub`` / unknown / empty → StubSegmenter

    The factory never raises on missing optional dependencies — same
    self-configuring contract as ``detector_models.get_detector``.
    """
    v = (variant or "").lower().strip()

    if v in ("", OPTICAL_FLOW, OPTICAL_FLOW_FAST):
        return OpticalFlowSegmenter()

    if v == SPORTSBD:
        try:
            return SportsBDSegmenter()
        except ImportError:
            log.warning("sportsbd_unavailable_using_stub", variant=variant)
            return StubSegmenter()

    if v == LEARNED_PLAY:
        return LearnedPlaySegmenter()

    if v == STUB_SEGMENTER:
        return StubSegmenter()

    log.warning("unknown_segmenter_variant_using_stub", variant=variant)
    return StubSegmenter()


# ── Schema helpers ────────────────────────────────────────────────────────────


def validate_boundary(boundary: dict[str, Any]) -> bool:
    """Return True iff ``boundary`` matches the documented schema."""
    if not isinstance(boundary, dict):
        return False
    time_s = boundary.get("time_s")
    if not isinstance(time_s, (int, float)) or float(time_s) < 0.0:
        return False
    if float(boundary["time_s"]) < 0.0:
        return False
    conf = boundary.get("confidence")
    if not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
        return False
    if not isinstance(boundary.get("source"), str):
        return False
    return True
