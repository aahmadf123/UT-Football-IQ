"""Stage-aware model router (Issue #73 — generalises Issue #16's pose-only router).

Each pipeline stage chooses a model variant based on the
``processing_jobs.priority`` field:

  Same-session (priority >= SAME_SESSION_PRIORITY = 10) → fast variants
    tuned to fit the period-break window.
  Nightly       (priority <  SAME_SESSION_PRIORITY)      → heavier variants
    that can spend more compute for higher quality.

Defaults live in ``DEFAULT_ROUTING`` below. They can be overridden by
pointing the ``MODEL_ROUTING_CONFIG`` environment variable at a JSON
file shaped like::

    {
      "detect":  {"same_session": "yolov8s",   "nightly": "yolov8m"},
      "pose":    {"same_session": "rtmpose-t", "nightly": "rtmpose-m"}
    }

Stages not mentioned in the override file fall back to ``DEFAULT_ROUTING``.

Pose routing from Issue #16 is preserved exactly:

    select_model("pose", SAME_SESSION_PRIORITY) == "rtmpose-t"
    select_model("pose", NIGHTLY_PRIORITY)      == "rtmpose-m"

Usage::

    from pipeline.model_router import select_model, build_routing_artifact

    variant = select_model("detect", job["priority"])
    artifact = build_routing_artifact("detect", job["priority"])
    # artifact == {"detect": "yolov8n"}  (or similar)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Pose-variant identifiers retained from Issue #16 for callers that need
# the constants directly.
RTMPOSE_FAST: str = "rtmpose-t"
RTMPOSE_MEDIUM: str = "rtmpose-m"

# Detect / track variants — Issue #74 adds SAM 3.1 as an experimental
# nightly-only option behind the ``ENABLE_SAM3_NIGHTLY`` env switch.
YOLOV8N: str = "yolov8n"
YOLOV8M: str = "yolov8m"
SAM3_1: str = "sam3.1"
IOU_TRACKER: str = "iou-tracker"
SAM3_MASK_TRACKER: str = "sam3-mask-tracker"

# Tracker variants — Issue #129. BoT-SORT (ECC camera-motion compensation +
# appearance ReID, ~2 GB) and StrongSORT (matching cascade + appearance EMA,
# best offline IDF1, ~3 GB) are heavier than the IoU tracker and rely on a
# ReID model at runtime. Neither has cleared a same-session benchmark on the
# production GTX 1660 Ti, so both stay nightly-only behind the guardrail (see
# ``NIGHTLY_ONLY_VARIANTS``). The same-session path keeps ``iou-tracker`` so
# the period-break window stays predictable. BoT-SORT can serve the *nightly*
# track bucket when ``ENABLE_BOTSORT_NIGHTLY`` is set; StrongSORT is reachable
# for the nightly bucket via ``MODEL_ROUTING_CONFIG``.
BOTSORT: str = "botsort"
STRONGSORT: str = "strongsort"

# Re-ID OCR variant — Issue #131. PARSeq reads small / rotated / motion-blurred
# jersey numbers far better than Tesseract but needs a torch model at runtime,
# so it is nightly-only. Same-session re-ID keeps the lightweight Tesseract
# ``jersey-ocr`` path. The PARSeq adapter falls back to Tesseract at runtime
# when its weights are unavailable, but the *routing decision* recorded in the
# audit is still ``parseq-ocr`` (mirrors how ``detect`` records ``yolov8m``
# even when a fallback fires).
JERSEY_OCR: str = "jersey-ocr"
PARSEQ_OCR: str = "parseq-ocr"

# Ball variant — Issue #133. A *dedicated* nano ball model (not a class on the
# player detector). Same model serves both priority buckets; SAHI tiling is
# gated by capture regime at the stage call site, not by priority (see
# ``pipeline.detection.ball_detector``). Pixel-only / lightweight, so it is
# NOT a nightly-only variant and is safe for the same-session window.
YOLO_BALL: str = "yolov8n-ball"

# Embedding variant — see ``docs/embeddings-architecture.md`` §11 and
# ``gpu-worker/pipeline/stage_embed.py``.
PLAY_EMBED_BASELINE: str = "play-embed-clip-vitb32-baseline"

# Calibration variants — Issue #127. Both are pixel-only OpenCV/NumPy paths
# (no heavy model inference). The lite variant runs white-paint + Hough +
# normalized DLT/RANSAC and is safe for the same-session window; the nightly
# variant additionally Kalman-smooths the per-window homography series. The
# deep-keypoint upgrade (PnLCalib / No-Bells-Just-Whistles) is a future
# nightly-only variant and is not yet bundled.
CALIB_HOUGH_DLT: str = "calib-hough-dlt"
CALIB_HOUGH_DLT_KALMAN: str = "calib-hough-dlt-kalman"

# Distilled DRONE_FOLLOW student detector — Issue #150. Produced by the nightly
# cross-regime self-distillation trainer (``training.cross_regime_distill``),
# which distills the high-quality FIXED_SIDELINE game pipeline (teacher) into the
# harder drone-follow practice regime (student). It is heavy/experimental, must
# clear a >= 5 pp drone-follow mAP gate before promotion, and is **only** valid
# for the ``drone_follow`` regime — so it is nightly-only and regime-gated (never
# selected for fixed_sideline; see ``select_detect_variant``).
DRONE_FOLLOW_DISTILLED: str = "yolov8m-drone-distilled"

# Capture-regime string the distilled student is gated to. Mirrors
# ``pipeline.homography.regime_detector.DRONE_FOLLOW`` without importing it, so
# this central router stays dependency-light.
DRONE_FOLLOW_REGIME: str = "drone_follow"

# Variants that are NEVER allowed on the same-session path because they
# are heavy / experimental / require a HF token at runtime, or — in the
# case of ``PLAY_EMBED_BASELINE`` — because their work product is only
# useful to retrospective search and would compete with detect/track/pose
# for the period-break window.  If a routing config tries to put one of
# these in the same_session bucket, the router falls back to the default
# same-session variant and logs a warning — see ``select_model``.
NIGHTLY_ONLY_VARIANTS: frozenset[str] = frozenset(
    {
        SAM3_1,
        SAM3_MASK_TRACKER,
        PLAY_EMBED_BASELINE,
        BOTSORT,
        STRONGSORT,
        PARSEQ_OCR,
        DRONE_FOLLOW_DISTILLED,
    }
)

# Deterministic workload-fusion injury-risk heuristic (Issue #149). Nightly-
# only: the fused score needs multi-day ACWR history that only the nightly
# rollup has. Registered here so a future learned model swaps in through the
# routing override file with no code change (#73).
WORKLOAD_FUSION_HEURISTIC: str = "acwr-asym-heuristic-v1"

# Returned for any stage that is not in the routing table.
UNKNOWN_STAGE_FALLBACK: str = "default"

# Priority bucket keys used inside the routing table.
_SAME_SESSION_KEY = "same_session"
_NIGHTLY_KEY = "nightly"

# When set to a truthy value, the nightly bucket for ``detect`` and
# ``track`` is upgraded to SAM 3.1 / SAM3-mask-tracker.  Default off:
# nightly stays on yolov8m + iou-tracker until SAM 3.1 has cleared the
# eval (see ``reports/phase2-issue74-sam3-eval.md``).
_SAM3_NIGHTLY_ENV = "ENABLE_SAM3_NIGHTLY"

# When set to a truthy value, the nightly bucket for ``track`` is upgraded
# from ``iou-tracker`` to BoT-SORT (Issue #129). Default off: nightly track
# stays on the IoU tracker until BoT-SORT clears the same-holdout IDF1 / ID-
# switch benchmark in the issue's acceptance criteria. If both this flag and
# ``ENABLE_SAM3_NIGHTLY`` are set, SAM 3.1's mask tracker wins the nightly
# track slot — it is tied to SAM 3.1's mask detections, so applying it last
# (see ``_build_routing``) gives it precedence.
_BOTSORT_NIGHTLY_ENV = "ENABLE_BOTSORT_NIGHTLY"

# When set to a truthy value, nightly ``drone_follow`` detect jobs route to the
# distilled DRONE_FOLLOW student (Issue #150). Unlike the SAM 3.1 / BoT-SORT
# nightly swaps, this is NOT a routing-table swap — the student is regime-
# specific, so applying it table-wide would wrongly hand fixed_sideline clips a
# drone-tuned detector. Instead the gate is enforced per-job in
# ``select_detect_variant`` (nightly AND drone_follow AND this flag). Default off:
# nightly detect stays on yolov8m for every regime until a distilled student has
# cleared the >= 5 pp drone-follow mAP gate and been promoted in the registry.
_DRONE_DISTILL_NIGHTLY_ENV = "ENABLE_DRONE_DISTILL_NIGHTLY"

# Default stage × priority routing.
#
# Same-session variants must comfortably fit the 5–10 minute period-break
# window on a GTX 1660 Ti class GPU. Nightly variants can be heavier.
DEFAULT_ROUTING: dict[str, dict[str, str]] = {
    "segment":    {_SAME_SESSION_KEY: "optical-flow-fast", _NIGHTLY_KEY: "optical-flow-fast"},
    "calibrate":  {_SAME_SESSION_KEY: CALIB_HOUGH_DLT,     _NIGHTLY_KEY: CALIB_HOUGH_DLT_KALMAN},
    "detect":     {_SAME_SESSION_KEY: "yolov8n",           _NIGHTLY_KEY: "yolov8m"},
    "ball":       {_SAME_SESSION_KEY: YOLO_BALL,           _NIGHTLY_KEY: YOLO_BALL},
    "track":      {_SAME_SESSION_KEY: IOU_TRACKER,         _NIGHTLY_KEY: IOU_TRACKER},
    "reid":       {_SAME_SESSION_KEY: JERSEY_OCR,          _NIGHTLY_KEY: PARSEQ_OCR},
    "pose":       {_SAME_SESSION_KEY: RTMPOSE_FAST,        _NIGHTLY_KEY: RTMPOSE_MEDIUM},
    "render":     {_SAME_SESSION_KEY: "ffmpeg-overlay",    _NIGHTLY_KEY: "ffmpeg-overlay"},
    "embeddings": {_SAME_SESSION_KEY: "none",              _NIGHTLY_KEY: PLAY_EMBED_BASELINE},
    "workload_fusion": {_SAME_SESSION_KEY: "none",         _NIGHTLY_KEY: WORKLOAD_FUSION_HEURISTIC},
}


def _load_override() -> dict[str, dict[str, str]]:
    """Read the JSON override file pointed to by ``MODEL_ROUTING_CONFIG``.

    Returns an empty dict if the env var is unset or the file is missing /
    malformed (with a warning logged) so the worker never crashes on a bad
    config drop.
    """
    path_str = os.environ.get("MODEL_ROUTING_CONFIG")
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.is_file():
        log.warning("model_router_override_missing", path=path_str)
        return {}
    try:
        raw: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("model_router_override_unreadable", path=path_str, error=str(exc))
        return {}
    if not isinstance(raw, dict):
        log.warning("model_router_override_invalid_shape", path=path_str)
        return {}
    return raw


def _sam3_nightly_enabled() -> bool:
    """Whether SAM 3.1 + mask tracker should serve the nightly detect/track buckets."""
    raw = os.environ.get(_SAM3_NIGHTLY_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _botsort_nightly_enabled() -> bool:
    """Whether BoT-SORT should serve the nightly ``track`` bucket."""
    raw = os.environ.get(_BOTSORT_NIGHTLY_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _drone_distill_nightly_enabled() -> bool:
    """Whether the distilled DRONE_FOLLOW student should serve nightly ``detect``."""
    raw = os.environ.get(_DRONE_DISTILL_NIGHTLY_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _apply_botsort_nightly(
    routing: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Swap the nightly ``track`` variant to BoT-SORT when the env flag is on.

    Same-session ``track`` is never touched: even with the flag enabled,
    period-break clips keep ``iou-tracker`` so latency stays predictable.
    Applied *before* ``_apply_sam3_nightly`` so that when both flags are set
    the SAM 3.1 mask tracker (tied to SAM 3.1 mask detections) wins the slot.
    """
    if not _botsort_nightly_enabled():
        return routing
    track = dict(routing.get("track", {}))
    track[_NIGHTLY_KEY] = BOTSORT
    routing["track"] = track
    log.info("botsort_nightly_enabled", track_nightly=BOTSORT)
    return routing


def _apply_sam3_nightly(
    routing: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Swap detect/track nightly variants to SAM 3.1 when the env flag is on.

    Same-session entries are never touched: even when the experimental
    flag is enabled, period-break clips continue to use yolov8n +
    iou-tracker so latency remains predictable.
    """
    if not _sam3_nightly_enabled():
        return routing
    detect = dict(routing.get("detect", {}))
    detect[_NIGHTLY_KEY] = SAM3_1
    routing["detect"] = detect
    track = dict(routing.get("track", {}))
    track[_NIGHTLY_KEY] = SAM3_MASK_TRACKER
    routing["track"] = track
    log.info(
        "sam3_nightly_enabled",
        detect_nightly=SAM3_1,
        track_nightly=SAM3_MASK_TRACKER,
    )
    return routing


def _enforce_same_session_safety(
    routing: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Block nightly-only variants from leaking into the same-session bucket.

    If an override file puts ``sam3.1`` or ``sam3-mask-tracker`` in
    same_session, replace it with the matching default and log loudly.
    Hard guarantee: nothing in ``NIGHTLY_ONLY_VARIANTS`` ever ships to
    same-session, regardless of config.
    """
    for stage, variants in routing.items():
        ss = variants.get(_SAME_SESSION_KEY)
        if ss in NIGHTLY_ONLY_VARIANTS:
            default_ss = DEFAULT_ROUTING.get(stage, {}).get(_SAME_SESSION_KEY)
            log.warning(
                "model_router_blocked_nightly_only_in_same_session",
                stage=stage,
                attempted=ss,
                replaced_with=default_ss,
            )
            if default_ss is not None:
                variants[_SAME_SESSION_KEY] = default_ss
            else:
                variants.pop(_SAME_SESSION_KEY, None)
    return routing


def _build_routing() -> dict[str, dict[str, str]]:
    """Merge ``DEFAULT_ROUTING`` with any JSON override.

    Per-stage merge: overridden stages fully replace their default entry,
    but stages not mentioned in the override keep their defaults.  Then
    apply the SAM 3.1 nightly swap (if enabled) and enforce the
    same-session safety guard.
    """
    routing: dict[str, dict[str, str]] = {
        stage: dict(variants) for stage, variants in DEFAULT_ROUTING.items()
    }
    for stage, variants in _load_override().items():
        if not isinstance(variants, dict):
            log.warning("model_router_override_stage_invalid", stage=stage)
            continue
        merged = dict(routing.get(stage, {}))
        for key, value in variants.items():
            if isinstance(value, str):
                merged[key] = value
        routing[stage] = merged
    routing = _apply_botsort_nightly(routing)
    routing = _apply_sam3_nightly(routing)
    routing = _enforce_same_session_safety(routing)
    return routing


# Resolved once at import; tests that mutate ``MODEL_ROUTING_CONFIG`` should
# call ``reload_routing()`` (or reload the module) to pick up changes.
ROUTING: dict[str, dict[str, str]] = _build_routing()


def reload_routing() -> dict[str, dict[str, str]]:
    """Re-read overrides and rebuild ``ROUTING``. Exposed for tests."""
    global ROUTING
    ROUTING = _build_routing()
    return ROUTING


def _priority_key(priority: int) -> str:
    return _SAME_SESSION_KEY if priority >= SAME_SESSION_PRIORITY else _NIGHTLY_KEY


def select_model(stage: str, priority: int) -> str:
    """Return the model variant for ``stage`` at the given ``priority``.

    Args:
        stage: Pipeline stage name — one of ``segment``, ``calibrate``,
            ``detect``, ``ball``, ``track``, ``reid``, ``pose``, ``render``,
            ``embeddings``, or any key configured via ``MODEL_ROUTING_CONFIG``.
        priority: Value from ``processing_jobs.priority``.
            ``SAME_SESSION_PRIORITY`` (10) routes to the fast variant;
            anything lower routes to the nightly variant.

    Returns:
        The model variant string. Unknown stages return
        ``UNKNOWN_STAGE_FALLBACK`` and log a warning rather than raising.
    """
    bucket = _priority_key(priority)
    variants = ROUTING.get(stage)
    if variants is None:
        log.warning(
            "model_router_unknown_stage",
            stage=stage,
            priority=priority,
            fallback=UNKNOWN_STAGE_FALLBACK,
        )
        return UNKNOWN_STAGE_FALLBACK

    variant = variants.get(bucket) or variants.get(_NIGHTLY_KEY) or UNKNOWN_STAGE_FALLBACK
    log.info(
        "model_router_select",
        stage=stage,
        model=variant,
        priority=priority,
        bucket=bucket,
    )
    return variant


def build_routing_artifact(stage: str, priority: int) -> dict[str, str]:
    """Return ``{stage: variant}`` for persistence in ``output_artifacts``.

    The dispatcher merges this dict into
    ``processing_jobs.output_artifacts["model_routing"]`` so every
    completed job records which model variant served it.
    """
    return {stage: select_model(stage, priority)}


def select_detect_variant(priority: int, capture_regime: str | None) -> str:
    """Regime-aware ``detect`` variant selection (Issue #150).

    Returns the distilled DRONE_FOLLOW student
    (:data:`DRONE_FOLLOW_DISTILLED`) only when **all** hold: the job is nightly,
    ``capture_regime == "drone_follow"``, and ``ENABLE_DRONE_DISTILL_NIGHTLY`` is
    set. Otherwise it delegates to ``select_model("detect", priority)``. This
    keeps the student DRONE_FOLLOW-only — it can never serve a fixed_sideline
    clip — honoring the two-regime design while leaving ``select_model``'s
    signature untouched. Stages call this instead of ``select_model("detect", …)``
    so the audit trail still records the variant that actually ran.
    """
    if (
        is_nightly(priority)
        and capture_regime == DRONE_FOLLOW_REGIME
        and _drone_distill_nightly_enabled()
    ):
        log.info(
            "select_detect_variant_distilled",
            priority=priority,
            capture_regime=capture_regime,
            model=DRONE_FOLLOW_DISTILLED,
        )
        return DRONE_FOLLOW_DISTILLED
    return select_model("detect", priority)


def is_same_session(priority: int) -> bool:
    """Return True if ``priority`` qualifies as same-session."""
    return priority >= SAME_SESSION_PRIORITY


def is_nightly(priority: int) -> bool:
    """Return True if ``priority`` qualifies as a nightly full-quality run."""
    return priority <= NIGHTLY_PRIORITY


def is_nightly_only_variant(variant: str) -> bool:
    """Return True if ``variant`` must never serve a same-session job."""
    return variant in NIGHTLY_ONLY_VARIANTS
