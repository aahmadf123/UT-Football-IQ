"""Lightweight-path configuration for same-session period-break processing.

Same-session jobs must complete within the ~8 minute period-break window on a
GTX 1660 Ti (6 GB VRAM).  This module centralises the configuration that
governs what the lightweight path does differently from the nightly full run.

Routing decisions respect Issues #74 and #76:
  - Same-session detection: yolov8n (not SAM 3.1)
  - Same-session pose: rtmpose-t (not rtmpose-m)
  - Same-session render: 540 p overlay via ``period_renderer`` (not full HLS)
  - Embeddings are nightly-only

After the same-session lightweight path completes:
  1. A nightly follow-up ``render_hls`` job is queued to produce the full-
     resolution HLS package.
  2. Nightly full-quality re-processing can be scheduled on a T4 16 GB GPU.

Environment variables:
  SAME_SESSION_RENDER_HEIGHT — overlay height for period-break delivery (default: 540)
  SAME_SESSION_SKIP_STAGES  — comma-separated stages to skip on the lightweight
                               path (default: embeddings,self_scout)
"""

from __future__ import annotations

import os

import structlog

log = structlog.get_logger(__name__)

SAME_SESSION_RENDER_HEIGHT: int = int(
    os.environ.get("SAME_SESSION_RENDER_HEIGHT", "540")
)

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
    "pose",
    "events",
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
    "pose",
    "events",
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
