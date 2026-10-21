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
from pipeline.runtime import adapter_provenance
from pipeline.video_timeline import VideoTimeline, iter_frames

log = structlog.get_logger(__name__)

MODEL_PATH = os.environ.get("MODEL_DETECT_PATH", "yolov8n.pt")
DETECTION_CONF = float(os.environ.get("DETECTION_CONF", "0.35"))
FRAME_STEP = int(os.environ.get("DETECT_FRAME_STEP", "3"))  # CPU default: every 3rd frame
#: Detections per second to aim for on a GPU. Tracking quality depends on how
#: far players move between looks: every 3rd frame fragmented a drone clip's
#: ~22 players into 94 tracks; every frame (30 fps) roughly tripled track
#: length. 60 fps game film is detected every other frame.
GPU_TARGET_DETECT_FPS = 30.0


def detection_frame_step(fps: float, device: str | None = None) -> int:
    """Run the detector every N source frames.

    ``DETECT_FRAME_STEP`` always wins; otherwise a GPU detects at about
    ``GPU_TARGET_DETECT_FPS`` and the CPU keeps the cheaper default.
    """
    configured = os.environ.get("DETECT_FRAME_STEP")
    if configured:
        return max(1, int(configured))
    from pipeline.runtime import inference_device

    if (device or inference_device()).startswith(("cuda", "mps")):
        return max(1, round((fps or 30.0) / GPU_TARGET_DETECT_FPS))
    return max(1, FRAME_STEP)


def detection_batch_size(device: str | None = None) -> int:
    """Frames per detector call.

    A single-frame call leaves an RTX 5060 waiting on Python and transfer
    overhead: yolov8m on 720p game film ran 97 frames/s one at a time and
    135 frames/s in batches of 16, with the same boxes. ``DETECT_BATCH``
    always wins; the CPU gains nothing from batching.
    """
    configured = os.environ.get("DETECT_BATCH")
    if configured:
        return max(1, int(configured))
    from pipeline.runtime import inference_device

    return 16 if (device or inference_device()).startswith("cuda") else 1


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
    timeline: VideoTimeline | None = None,
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
            video_path,
            player,
            ball,
            suppressor,
            # A pixel-space polygon from one frame moves during a pan/zoom.
            # Applying it to a moving recording rejects real on-field players.
            boundary_from_diagnostics(field_boundary)
            if capture_regime == "fixed_sideline"
            else None,
            timeline=timeline,
        )
    finally:
        video_path.unlink(missing_ok=True)

    result["model_routing"] = {"detect": player_variant, "ball": ball_variant}
    result["model_provenance"] = {
        "detect": adapter_provenance(player, player_variant),
        "ball": adapter_provenance(ball, ball_variant),
    }
    result["data_source"] = result["model_provenance"]["detect"]["source"]
    if served_version_id:
        # Record which registry-promoted artifact actually served this run so
        # provenance survives into job output_artifacts.
        result["served_model_versions"] = {"detect": served_version_id}
    result["detection_strategy"] = {
        "capture_regime": capture_regime or "unknown",
        "player": player_detection_strategy(capture_regime),
        "ball": ball_strategy(capture_regime) if ball is not None else "disabled",
        "official_suppression": suppressor.enabled,
        "boundary_filter": "static"
        if capture_regime == "fixed_sideline" and field_boundary
        else "disabled",
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
    *,
    timeline: VideoTimeline | None = None,
) -> dict[str, Any]:
    from pipeline.hwaccel import nvdec_video_capture

    cap = nvdec_video_capture(video_path)
    fps: float = cap.get(cv2.CAP_PROP_FPS) or 30.0
    timeline = timeline or VideoTimeline.from_path(
        video_path,
        fps_fallback=fps,
        frame_count_fallback=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
    )

    detections: dict[str, list[dict[str, Any]]] = {}
    frame_step = detection_frame_step(fps)
    log.info("detect_frame_step", frame_step=frame_step, source_fps=round(fps, 2))
    frame_idx = 0
    counts = {"masked": 0, "official": 0, "ball": 0, "off_field": 0}
    batch_size = detection_batch_size()
    pending: list[tuple[int, Any]] = []

    def flush() -> None:
        # One GPU call per batch; per-frame post-processing is unchanged.
        frames = [frame for _, frame in pending]
        player_batches = player.detect_batch(frames)
        ball_batches = ball.detect_batch(frames) if ball is not None else [[] for _ in frames]
        for (index, frame), player_dets, ball_dets in zip(
            pending, player_batches, ball_batches, strict=True
        ):
            player_dets = suppressor.suppress(frame, player_dets)
            player_dets, dropped = _off_field(player_dets, boundary)
            counts["off_field"] += dropped
            counts["official"] += sum(
                1 for d in player_dets if d.get("class") in ("official", "sideline")
            )
            counts["ball"] += len(ball_dets)
            frame_dets: list[dict[str, Any]] = [*player_dets, *ball_dets]
            if frame_dets:
                for detection in frame_dets:
                    detection["timestamp_seconds"] = timeline.timestamp(index)
                    detection["timestamp_verified"] = timeline.verified
                detections[str(index)] = frame_dets
                if any("mask" in d for d in frame_dets):
                    counts["masked"] += 1
        pending.clear()

    try:
        # Frames are numbered by their timestamp, never by counting: decoders
        # drop frames they cannot reconstruct (see video_timeline). Frames
        # between detections are decoded but never copied out.
        for index, frame in iter_frames(
            cap, timeline, 0, None, wanted=lambda i: i % frame_step == 0, fill_gaps=False
        ):
            pending.append((index, frame))
            frame_idx = index + 1
            if len(pending) >= batch_size:
                flush()
        if pending:
            flush()
        frame_idx = max(frame_idx, len(timeline.timestamps))

    finally:
        cap.release()
    log.info(
        "stage_detect_done",
        frame_count=frame_idx,
        detection_frame_count=len(detections),
        masked_frame_count=counts["masked"],
        official_detections=counts["official"],
        off_field_detections=counts["off_field"],
        ball_detections=counts["ball"],
        produces_masks=player.produces_masks,
    )
    return {
        "fps": fps,
        "source_timeline": timeline.to_dict(),
        "total_frames": frame_idx,
        "detections": detections,
        "produces_masks": player.produces_masks,
        "off_field_detections": counts["off_field"],
    }
