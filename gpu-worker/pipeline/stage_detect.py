"""Stage 4 — Player + Ball Detection.

Composes router-resolved detectors into a regime-aware detection stack:

* **Players** (Issue #128): the model router picks the base variant
  (``yolov8n`` same-session / ``yolov8m`` nightly). On ``drone_follow`` clips
  the base is wrapped in dual-resolution + SAHI (400 px tiles) to recover the
  30–80 px players a single full-frame pass misses; on ``fixed_sideline`` /
  ``unknown`` the base detector runs directly.
* **Ball** (Issue #133): a *dedicated* nano ball model
  (``select_model("ball", priority)``), wrapped in SAHI (128 px tiles) only
  on ``drone_follow`` where the ball is 6–18 px, full-frame otherwise.
* **Officials** (Issue #148): striped officials are relabeled
  ``class = "official"`` (and optionally off-field figures ``"sideline"``).
  Relabeling never drops a box, so a false positive costs a relabel, not a
  deleted player; ``stage_labels`` filters these out before team/formation
  analysis.

Every model choice flows through ``select_model``; the per-stage routing
decision is recorded in ``output_artifacts["model_routing"]`` (merged
additively by the dispatcher) and the regime/slicing detail in
``output_artifacts["detection_strategy"]``.

Output schema is backward compatible — adapters that produce masks add an
optional ``mask`` field; bbox-only consumers keep working:

  {
    "<frame_number>": [
      {"bbox": [x1,y1,x2,y2], "confidence": 0.82, "class": "player"},
      {"bbox": [...], "confidence": ..., "class": "ball"},
      {"bbox": [...], "confidence": ..., "class": "official"},
      ...
    ]
  }

Note: This stage does NOT write to tracklets — that is Stage 5.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2
import structlog

from pipeline import object_store
from pipeline.detection.ball_detector import (
    ball_strategy,
    build_ball_detector,
    get_ball_detector,
)
from pipeline.detection.official_suppressor import OfficialSuppressor
from pipeline.detector_models import (
    DetectorBase,
    build_player_detector,
    get_detector,
    player_detection_strategy,
)
from pipeline.homography.field_boundary import FieldBoundary, boundary_from_diagnostics
from pipeline.homography.project import ground_anchor
from pipeline.model_router import select_model

log = structlog.get_logger(__name__)

MODEL_PATH = os.environ.get("MODEL_DETECT_PATH", "yolov8n.pt")
DETECTION_CONF = float(os.environ.get("DETECTION_CONF", "0.35"))
FRAME_STEP = int(os.environ.get("DETECT_FRAME_STEP", "3"))  # run every N frames

# Ball detection runs alongside players unless explicitly disabled.
DETECT_BALL_ENABLED = os.environ.get("DETECT_BALL_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def run(
    video_id: str,
    input_uri: str,
    job_id: str,
    *,
    detector: DetectorBase | None = None,
    variant: str | None = None,
    capture_regime: str | None = None,
    priority: int = 0,
    ball_detector: DetectorBase | None = None,
    suppressor: OfficialSuppressor | None = None,
    field_boundary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run Stage 4 detection and return per-frame detections + routing.

    Args:
        video_id, input_uri, job_id: pipeline-standard arguments.
        detector: Pre-built player detector; overrides ``variant``/regime
                  composition. Tests inject a ``StubDetector`` here.
        variant:  Player routing variant id (e.g. ``"yolov8n"``). When omitted
                  and no ``detector`` is injected, ``MODEL_DETECT_PATH`` selects
                  a YOLO adapter — preserving prior behaviour.
        capture_regime: ``drone_follow`` / ``fixed_sideline`` / ``unknown``
                  (from ingest, Issue #126). Gates SAHI / dual-resolution.
        priority: ``processing_jobs.priority`` — routes the ball variant.
        ball_detector: Pre-built ball detector; overrides ball composition.
        suppressor: Pre-built :class:`OfficialSuppressor`; defaults to one
                  configured from the environment.
        field_boundary: Serialised boundary from ``stage_calibrate``. When it
                  contains a real touchline, detections whose feet fall off the
                  playing surface are dropped -- bench, staff and spectators are
                  people the detector is right to find and wrong to track.
    """
    player_variant = variant or "yolov8n"
    log.info(
        "stage_detect_start",
        video_id=video_id,
        variant=player_variant,
        capture_regime=capture_regime,
    )

    # ── Player detector ───────────────────────────────────────────────────
    served_version_id: str | None = None
    if detector is None:
        # Serving leg: a human-promoted production detector from the model
        # registry takes precedence over bundled defaults (offline-safe —
        # resolve() returns None whenever the registry/artifact is absent).
        from pipeline import model_registry_client

        resolved = model_registry_client.resolve("detect")
        if resolved is not None:
            served_version_id = resolved.model_version_id
            base = get_detector(
                player_variant,
                model_path=str(resolved.local_path),
                confidence_threshold=DETECTION_CONF,
            )
            player = build_player_detector(player_variant, capture_regime, base=base)
        elif variant is None:
            # Legacy path: build the base detector from MODEL_DETECT_PATH and
            # let regime composition wrap it.
            base = get_detector(
                player_variant,
                model_path=MODEL_PATH,
                confidence_threshold=DETECTION_CONF,
            )
            player = build_player_detector(player_variant, capture_regime, base=base)
        else:
            player = build_player_detector(
                player_variant,
                capture_regime,
                confidence_threshold=DETECTION_CONF,
            )
    else:
        player = detector

    # ── Ball detector (dedicated model, regime-gated SAHI) ─────────────────
    ball_variant = select_model("ball", priority)
    ball: DetectorBase | None
    if ball_detector is not None:
        ball = ball_detector
    elif DETECT_BALL_ENABLED:
        # Same serving leg as the player detector: a promoted registry ball
        # model wins over the bundled MODEL_BALL_PATH default (offline-safe).
        from pipeline import model_registry_client

        resolved_ball = model_registry_client.resolve("ball")
        if resolved_ball is not None:
            base_ball = get_ball_detector(ball_variant, model_path=str(resolved_ball.local_path))
            ball = (
                build_ball_detector(ball_variant, capture_regime, base=base_ball)
                if base_ball is not None
                else None
            )
        else:
            ball = build_ball_detector(ball_variant, capture_regime)
    else:
        ball = None

    # ── Official suppressor ────────────────────────────────────────────────
    if suppressor is None:
        suppressor = OfficialSuppressor()

    object_key = _uri_to_object_key(input_uri)
    video_path = object_store.download_to_temp(object_key)
    try:
        result = _detect(
            video_path, player, ball, suppressor, boundary_from_diagnostics(field_boundary)
        )
    finally:
        video_path.unlink(missing_ok=True)

    result["model_routing"] = {"detect": player_variant, "ball": ball_variant}
    if served_version_id:
        # Record which registry-promoted artifact actually served this run so
        # provenance survives into job output_artifacts.
        result["served_model_versions"] = {"detect": served_version_id}
    result["detection_strategy"] = {
        "capture_regime": capture_regime or "unknown",
        "player": player_detection_strategy(capture_regime),
        "ball": ball_strategy(capture_regime) if ball is not None else "disabled",
        "official_suppression": suppressor.enabled,
    }
    return result


def _uri_to_object_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


def _off_field(
    detections: list[dict[str, Any]], boundary: FieldBoundary | None
) -> tuple[list[dict[str, Any]], int]:
    """Drop detections standing off the playing surface.

    Bench players, position coaches, trainers, camera operators and spectators
    are all people, and the detector is right to find them -- they are simply
    not in the play. Left in, each becomes a tracklet that inflates formation
    counts, joins coverage shells, and accrues workload against whoever re-ID
    eventually guesses.

    Tested at the **ground anchor**, the bottom-centre of the box, because that
    is where the person meets the ground plane the boundary describes. A box
    centre sits a yard or so up the body and would keep someone standing just
    off the sideline.

    A no-op when no real touchline is in frame: the boundary is then the frame
    border, everything is nominally inside it, and rejecting anything would
    discard real players. Returns the kept detections and the drop count.
    """
    if boundary is None or not boundary.has_visible_boundary:
        return detections, 0
    kept = [
        det
        for det in detections
        if not det.get("bbox") or boundary.contains(ground_anchor(det["bbox"]))
    ]
    return kept, len(detections) - len(kept)


def _detect(
    video_path: Path,
    player: DetectorBase,
    ball: DetectorBase | None,
    suppressor: OfficialSuppressor,
    boundary: FieldBoundary | None = None,
) -> dict[str, Any]:
    from pipeline.hwaccel import nvdec_video_capture

    cap = nvdec_video_capture(video_path)
    fps: float = cap.get(cv2.CAP_PROP_FPS) or 30.0

    detections: dict[str, list[dict[str, Any]]] = {}
    frame_idx = 0
    masked_frame_count = 0
    official_count = 0
    ball_count = 0
    off_field_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % FRAME_STEP == 0:
            player_dets = player.detect(frame)
            player_dets = suppressor.suppress(frame, player_dets)
            player_dets, dropped = _off_field(player_dets, boundary)
            off_field_count += dropped
            official_count += sum(
                1 for d in player_dets if d.get("class") in ("official", "sideline")
            )

            frame_dets: list[dict[str, Any]] = list(player_dets)
            if ball is not None:
                ball_dets = ball.detect(frame)
                ball_count += len(ball_dets)
                frame_dets.extend(ball_dets)

            if frame_dets:
                detections[str(frame_idx)] = frame_dets
                if any("mask" in d for d in frame_dets):
                    masked_frame_count += 1

        frame_idx += 1

    cap.release()
    log.info(
        "stage_detect_done",
        frame_count=frame_idx,
        detection_frame_count=len(detections),
        masked_frame_count=masked_frame_count,
        official_detections=official_count,
        off_field_detections=off_field_count,
        ball_detections=ball_count,
        produces_masks=player.produces_masks,
    )
    return {
        "fps": fps,
        "total_frames": frame_idx,
        "detections": detections,
        "produces_masks": player.produces_masks,
        "off_field_detections": off_field_count,
    }
