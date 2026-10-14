"""CPU-first processing profiles and compatible priority routing.

Review prepares clean play media without inference models. Quick uses bounded
local detection/tracking and optional features; detailed adds available pose and
advanced analysis. Throughput depends on this computer, footage and installed
models and is measured during processing. Queue priority retains the existing
same-session/nightly model guardrail without promising a fixed turnaround time.

SAME_SESSION_RENDER_HEIGHT and SAME_SESSION_SKIP_STAGES preserve legacy caller
configuration. Profiles select capabilities independently of queue priority.
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)

SAME_SESSION_RENDER_HEIGHT: int = int(os.environ.get("SAME_SESSION_RENDER_HEIGHT", "540"))

_SKIP_RAW = os.environ.get("SAME_SESSION_SKIP_STAGES", "embeddings,self_scout")
SAME_SESSION_SKIP_STAGES: frozenset[str] = frozenset(
    s.strip() for s in _SKIP_RAW.split(",") if s.strip()
)

SAME_SESSION_STAGES: list[str] = [
    "ingest",
    "segment",
    "calibrate",
    "detect",
    "track",
    "reid",
    # events before pose: pose anchors its metrics at the snap this detects.
    # This ordering is load-bearing and is asserted against
    # orchestrator.CLIP_STAGES in the tests.
    "events",
    "pose",
    "labels",
    "metrics",
    "routes",
    "coverage",
    "oline",
    "pressure",
    "render",
]

NIGHTLY_STAGES: list[str] = [
    "ingest",
    "segment",
    "calibrate",
    "detect",
    "track",
    "reid",
    # events before pose: pose anchors its metrics at the snap this detects.
    # This ordering is load-bearing and is asserted against
    # orchestrator.CLIP_STAGES in the tests.
    "events",
    "pose",
    "labels",
    "metrics",
    "routes",
    "coverage",
    "oline",
    "pressure",
    "render",
    "render_hls",
    "self_scout",
    "embeddings",
    "workload_rollup",
]


def should_skip_stage(stage: str, priority: int) -> bool:
    """Return True if ``stage`` should be skipped on the same-session path."""
    from queue.same_session_queue import SAME_SESSION_PRIORITY

    if priority < SAME_SESSION_PRIORITY:
        return False
    return stage in SAME_SESSION_SKIP_STAGES


def use_period_renderer(priority: int) -> bool:
    """Return True if the render stage should use the lightweight period renderer."""
    from queue.same_session_queue import SAME_SESSION_PRIORITY

    return priority >= SAME_SESSION_PRIORITY


def should_queue_nightly_followup(priority: int) -> bool:
    """Return True if a nightly follow-up HLS job should be queued after same-session."""
    from queue.same_session_queue import SAME_SESSION_PRIORITY

    return priority >= SAME_SESSION_PRIORITY


# Profiles select product capabilities independently of queue priority. Model
# routing still enforces the existing same-session experimental-model guardrail.
PROCESSING_PROFILES: dict[str, tuple[str, ...]] = {
    "review": ("ingest", "segment", "playback_proxy"),
    "quick": (
        "ingest",
        "segment",
        "playback_proxy",
        "calibrate",
        "detect",
        "track",
        "reid",
        "events",
        "labels",
        "metrics",
        "routes",
        "coverage",
        "render",
    ),
    "detailed": (
        "ingest",
        "segment",
        "playback_proxy",
        "calibrate",
        "detect",
        "track",
        "reid",
        "events",
        "pose",
        "labels",
        "metrics",
        "routes",
        "coverage",
        "oline",
        "pressure",
        "render",
        "render_hls",
    ),
}


def processing_profile(value: str | None) -> str:
    selected = (value or os.environ.get("PROCESSING_PROFILE", "quick")).strip().lower()
    if selected not in PROCESSING_PROFILES:
        raise ValueError(f"Unknown processing profile: {selected!r}")
    return selected


def profile_stages(profile: str | None) -> list[str]:
    return list(PROCESSING_PROFILES[processing_profile(profile)])


def local_model_variant(stage: str, priority: int, profile: str) -> str:
    """Use bounded CPU variants; accelerated routing remains explicitly opt-in."""
    from pipeline import model_router

    device = os.environ.get("PIPELINE_DEVICE", "cpu").strip().lower()
    if device == "cpu":
        variants = {
            "detect": os.environ.get("PIPELINE_LOCAL_DETECT_VARIANT", "yolov8n"),
            "track": "iou-tracker",
            "reid": "jersey-ocr",
            "calibrate": model_router.CALIB_HOUGH_DLT,
        }
        if stage in variants:
            candidate = variants[stage]
            if priority >= 10 and model_router.is_nightly_only_variant(candidate):
                return model_router.select_model(stage, priority)
            return candidate
    return model_router.select_model(stage, priority)
