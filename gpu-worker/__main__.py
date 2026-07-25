"""GPU Worker — claims processing jobs and runs the video pipeline.

Two queue backends (``QUEUE_BACKEND``):

  ``db`` (default) — the backend's ``processing_jobs`` table IS the queue.
      The worker claims runnable rows via ``POST /api/v1/jobs/claim``
      (FOR UPDATE SKIP LOCKED server-side), heartbeats its lease, and runs
      ``pipeline`` jobs through :mod:`pipeline.orchestrator` with per-stage
      progress reported into the job row. No Cloudflare account needed.
  ``cf`` — legacy: long-poll the Cloudflare Queues HTTP API, one stage per
      message. Kept for existing deployments during the migration window.

Environment variables:
  QUEUE_BACKEND             — "db" (default) or "cf"
  BACKEND_API_URL           — backend base URL (required for db backend)
  WORKER_EMAIL / WORKER_PASSWORD — seeded service-account credentials
  WORKER_ID                 — claim identity (default: hostname-pid)
  GPU_WORKER_POLL_INTERVAL  — seconds between queue polls (default: 10)
  STORAGE_BACKEND / LOCAL_STORAGE_ROOT — see pipeline.storage
  R2_* / CLOUDFLARE_*       — cloud credentials (cf/r2 modes only)
  MODEL_DETECT_PATH         — path to YOLO weights (default: yolov8n.pt)
  MODEL_POSE_PATH           — path to RTMPose .pth weights (optional; stub used when absent)
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

import httpx
import structlog

# ── Logging setup ─────────────────────────────────────────────────────────────


def _add_service_context(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    event_dict.setdefault("service", "football-iq-gpu-worker")
    event_dict.setdefault("env", os.environ.get("ENVIRONMENT", "development"))
    return event_dict


structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_service_context,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logging.basicConfig(level=logging.INFO)
log = structlog.get_logger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
QUEUE_BACKEND = os.environ.get("QUEUE_BACKEND", "db").strip().lower()
QUEUE_NAME = os.environ.get("CF_QUEUE_VIDEO_PROCESSING", "video-processing-jobs")
POLL_INTERVAL = int(os.environ.get("GPU_WORKER_POLL_INTERVAL", "10"))
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "")
HEARTBEAT_INTERVAL = int(os.environ.get("GPU_WORKER_HEARTBEAT_INTERVAL", "60"))
LEASE_SECONDS = int(os.environ.get("GPU_WORKER_LEASE_SECONDS", "600"))


def _worker_id() -> str:
    from worker.auth import worker_id

    return worker_id()


def _cf_config() -> tuple[str, str, str]:
    """Cloudflare Queue credentials — only the cf backend requires them."""
    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    api_token = os.environ["CLOUDFLARE_API_TOKEN"]
    pull_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/queues/{QUEUE_NAME}/messages/pull"
    )
    return account_id, api_token, pull_url

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    log.info("shutdown_signal_received", signal=signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Cloudflare queue polling (cf backend only) ────────────────────────────────


def pull_messages(client: httpx.Client, batch_size: int = 5) -> list[dict[str, Any]]:
    """Pull up to `batch_size` messages from the Cloudflare Queue."""
    _account_id, api_token, pull_url = _cf_config()
    resp = client.post(
        pull_url,
        headers={"Authorization": f"Bearer {api_token}"},
        json={"batch_size": batch_size, "visibility_timeout_ms": 60_000},
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data.get("result", {}).get("messages", [])


def ack_message(client: httpx.Client, lease_id: str) -> None:
    """Acknowledge (delete) a processed message from the queue."""
    account_id, api_token, _pull_url = _cf_config()
    ack_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/queues/{QUEUE_NAME}/messages/ack"
    )
    resp = client.post(
        ack_url,
        headers={"Authorization": f"Bearer {api_token}"},
        json={"acks": [{"lease_id": lease_id}]},
        timeout=15,
    )
    resp.raise_for_status()


# ── DB queue (default backend) ────────────────────────────────────────────────


def _auth_headers() -> dict[str, str]:
    try:
        from worker import auth as worker_auth

        bearer = worker_auth.token()
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}
    except ImportError:
        pass
    return {}


def claim_db_job(client: httpx.Client) -> dict[str, Any] | None:
    """POST /api/v1/jobs/claim — returns the claimed job row or None."""
    resp = client.post(
        f"{BACKEND_API_URL}/api/v1/jobs/claim",
        headers=_auth_headers(),
        json={"worker_id": _worker_id(), "lease_seconds": LEASE_SECONDS},
        timeout=30,
    )
    if resp.status_code == 204:
        return None
    if resp.status_code == 401:
        from worker import auth as worker_auth

        worker_auth.invalidate()
        resp = client.post(
            f"{BACKEND_API_URL}/api/v1/jobs/claim",
            headers=_auth_headers(),
            json={"worker_id": _worker_id(), "lease_seconds": LEASE_SECONDS},
            timeout=30,
        )
        if resp.status_code == 204:
            return None
    resp.raise_for_status()
    return dict(resp.json())


def heartbeat_db_job(job_id: str, progress: dict[str, Any] | None = None) -> None:
    """Extend the job lease + merge progress. Best-effort, never raises."""
    try:
        resp = httpx.post(
            f"{BACKEND_API_URL}/api/v1/jobs/{job_id}/heartbeat",
            headers=_auth_headers(),
            json={
                "worker_id": _worker_id(),
                "lease_seconds": LEASE_SECONDS,
                "progress": progress or {},
            },
            timeout=15,
        )
        if resp.status_code == 401:
            from worker import auth as worker_auth

            worker_auth.invalidate()
    except Exception as exc:
        log.warning("heartbeat_failed", job_id=job_id, error=str(exc))


def _fetch_video(video_id: str) -> dict[str, Any] | None:
    """GET /api/v1/videos/{id} — resolve storage_uri for a DB-claimed job."""
    try:
        resp = httpx.get(
            f"{BACKEND_API_URL}/api/v1/videos/{video_id}",
            headers=_auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return dict(resp.json())
    except Exception as exc:
        log.warning("video_fetch_failed", video_id=video_id, error=str(exc))
        return None


def _db_row_to_job(row: dict[str, Any]) -> dict[str, Any]:
    """Map a claimed processing_jobs row onto the legacy job-dict shape."""
    input_artifacts = dict(row.get("input_artifacts") or {})
    # cf_trigger-created rows carry the camelCase queue payload in
    # input_artifacts; backend-created rows may use snake_case.
    input_uri = str(input_artifacts.get("input_uri") or input_artifacts.get("inputUri") or "")
    video_id = str(row.get("video_id") or "")
    if not input_uri and video_id:
        video = _fetch_video(video_id)
        if video:
            input_uri = str(video.get("storage_uri") or "")
    return {
        "jobId": str(row.get("id", "")),
        "jobType": str(row.get("job_type", "")),
        "videoId": video_id,
        "clipId": str(row.get("clip_id") or ""),
        "inputUri": input_uri,
        "priority": int(row.get("priority") or 0),
        "inputArtifacts": input_artifacts,
    }


def process_pipeline_job(job: dict[str, Any]) -> None:
    """Run a full-chain ``pipeline`` job via the orchestrator.

    Per-stage progress flows into the job row through heartbeats; a
    background thread keeps the lease alive between stage transitions.
    """
    import threading

    from pipeline import backend as backend_client
    from pipeline.orchestrator import StorageArtifactSink, run_pipeline
    from worker.observability import (
        record_job_failed,
        record_job_started,
        record_job_succeeded,
    )

    job_id = job["jobId"]
    video_id = job["videoId"]
    input_uri = job["inputUri"]
    priority = int(job.get("priority", 0))
    pipeline_mode = "same_session" if priority >= 10 else "nightly"

    if not input_uri:
        _update_job_status(job_id, "failed", error_message="no input_uri resolvable")
        return

    record_job_started("pipeline", pipeline_mode)
    started = time.time()
    progress_state: dict[str, Any] = {}
    progress_lock = threading.Lock()
    stop_heartbeat = threading.Event()

    def progress_cb(stage: str, clip_id: str | None, status: str, extra: dict[str, Any]) -> None:
        key = f"{stage}:{clip_id[:8]}" if clip_id else stage
        with progress_lock:
            progress_state[key] = {"status": status, **extra}
            snapshot = dict(progress_state)
        # Stage transitions double as lease extensions.
        heartbeat_db_job(job_id, snapshot)

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(HEARTBEAT_INTERVAL):
            with progress_lock:
                snapshot = dict(progress_state)
            heartbeat_db_job(job_id, snapshot)

    hb_thread = threading.Thread(target=heartbeat_loop, name="job-heartbeat", daemon=True)
    hb_thread.start()
    try:
        summary = run_pipeline(
            video_id,
            input_uri,
            priority=priority,
            job_id=job_id,
            progress_cb=progress_cb,
            artifact_sink=StorageArtifactSink(video_id),
        )
        _update_job_status(job_id, "succeeded", output_artifacts=summary)
        backend_client.patch_video_status(video_id, "ready")
        record_job_succeeded("pipeline", pipeline_mode, time.time() - started)
        log.info(
            "pipeline_job_succeeded",
            job_id=job_id,
            clip_count=summary.get("clip_count"),
            duration_seconds=round(time.time() - started, 2),
        )
    except Exception as exc:
        record_job_failed("pipeline", pipeline_mode)
        log.error("pipeline_job_failed", job_id=job_id, error=str(exc))
        _update_job_status(job_id, "failed", error_message=str(exc))
        backend_client.patch_video_status(video_id, "failed")
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=5)


# ── Job processing ────────────────────────────────────────────────────────────


def process_job(job: dict[str, Any]) -> None:
    """Dispatch a single job to the appropriate processing stage.

    Same-session jobs (priority >= SAME_SESSION_PRIORITY) are wrapped in the
    8-minute timeout handler.  If the job exceeds the deadline it is requeued
    for nightly processing and the backend job record is marked failed.
    """
    from pipeline import model_router
    from pipeline.lightweight_config import should_skip_stage
    from worker.timeout_handler import JobTimeoutError, run_with_timeout

    job_id: str = job.get("jobId", "unknown")
    job_type: str = job.get("jobType", "")
    video_id: str = job.get("videoId", "")
    clip_id: str = job.get("clipId", "")
    input_uri: str = job.get("inputUri", "")
    priority: int = int(job.get("priority", 0))
    input_artifacts: dict[str, Any] = job.get("inputArtifacts", {})
    is_same_session = model_router.is_same_session(priority)

    if job_type == "pipeline":
        # Full-chain job: the orchestrator owns stage sequencing, progress,
        # and terminal status updates.
        process_pipeline_job(job)
        return

    pipeline_mode = "same_session" if is_same_session else "nightly"
    log.info(
        "processing_job",
        job_id=job_id,
        job_type=job_type,
        video_id=video_id,
        clip_id=clip_id,
        priority=priority,
        pipeline_mode=pipeline_mode,
    )

    from worker.observability import (
        record_heartbeat as _hb,
    )
    from worker.observability import (
        record_job_failed as _jf,
    )
    from worker.observability import (
        record_job_started as _js,
    )
    from worker.observability import (
        record_job_succeeded as _jsuc,
    )
    from worker.observability import (
        record_job_timed_out as _jto,
    )

    _js(job_type, pipeline_mode)
    _hb()
    job_start_time = time.time()

    if should_skip_stage(job_type, priority):
        log.info("stage_skipped_lightweight_path", job_type=job_type, job_id=job_id)
        _update_job_status(
            job_id,
            "succeeded",
            output_artifacts={
                "skipped": True,
                "reason": "same_session_lightweight_path",
            },
        )
        _jsuc(job_type, pipeline_mode, time.time() - job_start_time)
        return

    _update_job_status(job_id, "running")

    try:
        if is_same_session:
            artifacts = run_with_timeout(
                _dispatch,
                args=(
                    job_type,
                    video_id,
                    clip_id,
                    input_uri,
                    input_artifacts,
                    job_id,
                    priority,
                ),
                job_id=job_id,
                job_payload=job,
            )
        else:
            artifacts = _dispatch(
                job_type,
                video_id,
                clip_id,
                input_uri,
                input_artifacts,
                job_id,
                priority,
            )
        artifacts = dict(artifacts or {})
        # ``build_routing_artifact`` is a priority-only fallback so every job
        # records a variant. A stage that already recorded its own routing wins —
        # e.g. the detect stage records the regime-aware variant actually used
        # (the distilled DRONE_FOLLOW student, Issue #150), which the regime-blind
        # fallback would otherwise clobber. So fill missing keys, never overwrite.
        routing = model_router.build_routing_artifact(job_type, priority)
        stage_routing = artifacts.setdefault("model_routing", {})
        for _stage, _variant in routing.items():
            stage_routing.setdefault(_stage, _variant)
        if is_same_session:
            artifacts["pipeline_mode"] = "same_session"
        _update_job_status(job_id, "succeeded", output_artifacts=artifacts)
        _jsuc(job_type, pipeline_mode, time.time() - job_start_time)
        log.info(
            "job_succeeded",
            job_id=job_id,
            duration_seconds=round(time.time() - job_start_time, 2),
        )

        # After a same-session render completes, queue the nightly HLS follow-up.
        if is_same_session and job_type == "render":
            _queue_nightly_hls_followup(job, artifacts)

        # After a nightly render completes, upgrade this video's same-session
        # preliminary clips to final (Issue #147): the nightly full-quality
        # output now supersedes the period-break first pass, so the coach's
        # "Preliminary" badge clears. Best-effort + idempotent; skipped when the
        # job carries no video_id.
        if not is_same_session and job_type == "render" and video_id:
            _finalize_nightly_clips(video_id)

    except JobTimeoutError:
        _jto(job_type, pipeline_mode)
        log.warning("job_timeout_handled", job_id=job_id, priority=priority)
    except Exception as exc:
        _jf(job_type, pipeline_mode)
        log.error("job_failed", job_id=job_id, error=str(exc))
        _update_job_status(job_id, "failed", error_message=str(exc))


def _dispatch(
    job_type: str,
    video_id: str,
    clip_id: str,
    input_uri: str,
    input_artifacts: dict[str, Any],
    job_id: str,
    priority: int = 0,
) -> dict[str, Any]:
    """Route to the correct pipeline stage module."""
    from pipeline import (
        model_router,
        stage_calibrate,
        stage_coverage,
        stage_detect,
        stage_events,
        stage_ingest,
        stage_labels,
        stage_metrics,
        stage_oline,
        stage_pressure,
        stage_reid,
        stage_render,
        stage_routes,
        stage_segment,
        stage_self_scout,
        stage_track,
    )
    from pipeline import (
        r2 as r2_mod,
    )

    if job_type == "ingest":
        return stage_ingest.run(video_id, input_uri, job_id)

    elif job_type == "segment":
        return stage_segment.run(video_id, input_uri, job_id, priority=priority)

    elif job_type == "calibrate":
        calib_variant = model_router.select_model("calibrate", priority)
        capture_regime = input_artifacts.get("capture_regime")
        return stage_calibrate.run(
            video_id,
            input_uri,
            job_id,
            variant=calib_variant,
            capture_regime=capture_regime,
        )

    elif job_type == "detect":
        capture_regime = input_artifacts.get("capture_regime")
        # Regime-aware: nightly drone_follow jobs may route to the distilled
        # student (Issue #150); everything else falls back to the priority table.
        detect_variant = model_router.select_detect_variant(priority, capture_regime)
        return stage_detect.run(
            video_id,
            input_uri,
            job_id,
            variant=detect_variant,
            capture_regime=capture_regime,
            priority=priority,
        )

    elif job_type == "track":
        detections: dict[str, Any] = input_artifacts.get("detections", {})
        fps: float = float(input_artifacts.get("fps", 30))
        track_variant = model_router.select_model("track", priority)
        return stage_track.run(clip_id, detections, fps, job_id, variant=track_variant)

    elif job_type == "reid":
        tracklet_ids: list[str] = input_artifacts.get("tracklet_ids", [])
        tracklets: list[dict[str, Any]] = input_artifacts.get("tracklets", [])
        roster: list[dict[str, Any]] = input_artifacts.get("roster", [])
        reid_variant = model_router.select_model("reid", priority)
        video_path = r2_mod.download_to_temp(_uri_to_r2_key(input_uri))
        try:
            return stage_reid.run(
                clip_id,
                video_path,
                tracklet_ids,
                tracklets,
                roster,
                BACKEND_API_URL,
                variant=reid_variant,
                priority=priority,
            )
        finally:
            video_path.unlink(missing_ok=True)

    elif job_type == "events":
        detections = input_artifacts.get("detections", {})
        fps = float(input_artifacts.get("fps", 30))
        # pose_by_frame comes from the job JSON so its frame keys are strings;
        # normalise to int so snap_frame arithmetic and dict lookups work.
        _pose_raw = input_artifacts.get("pose_by_frame")
        _pose_normed: dict[int, dict[str, dict[str, tuple[float, float]]]] | None = (
            {int(k): v for k, v in _pose_raw.items()} if _pose_raw else None
        )
        # Optional multi-signal inputs (Issues #132/#134). When absent the
        # stage falls back to the legacy bbox-displacement heuristic.
        return stage_events.run(
            clip_id,
            detections,
            fps,
            job_id,
            tracklets=input_artifacts.get("tracklets"),
            ball_detections=input_artifacts.get("ball_detections"),
            pose_by_frame=_pose_normed,
            frames=input_artifacts.get("frames"),
            frames_start_frame=int(input_artifacts.get("frames_start_frame", 0)),
            ol_track_ids=input_artifacts.get("ol_track_ids"),
            qb_track_id=input_artifacts.get("qb_track_id"),
            center_track_id=input_artifacts.get("center_track_id"),
            defender_ids=input_artifacts.get("defender_ids"),
            homography=input_artifacts.get("homography"),
            los_band_px=input_artifacts.get("los_band_px"),
            los_prior=input_artifacts.get("los_prior"),
            end_of_play_frame=input_artifacts.get("end_of_play_frame"),
            goal_line_x=input_artifacts.get("goal_line_x"),
        )

    elif job_type == "labels":
        tracklets = input_artifacts.get("tracklets", [])
        events_list: list[dict[str, Any]] = input_artifacts.get("events", [])
        fps = float(input_artifacts.get("fps", 30))
        return stage_labels.run(clip_id, tracklets, events_list, fps, input_uri)

    elif job_type == "metrics":
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        analytics_safe: bool = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))
        return stage_metrics.run(
            clip_id,
            tracklets,
            events_list,
            analytics_safe,
            fps,
            job_id,
            pose_metrics=input_artifacts.get("pose_metrics"),
        )

    elif job_type == "workload_rollup":
        # Nightly per-player workload rollup (Issue #149). Dispatched by the
        # Cloudflare cron trigger; reads its inputs from the backend API, so
        # there is no input_uri. Defaults to yesterday (UTC) when the cron
        # payload somehow lacks a date.
        from datetime import UTC, datetime, timedelta

        from pipeline import stage_workload_rollup

        rollup_date = input_artifacts.get("date") or (
            datetime.now(UTC).date() - timedelta(days=1)
        ).isoformat()
        return stage_workload_rollup.run(rollup_date, job_id, priority=priority)

    elif job_type == "routes":
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        fps = float(input_artifacts.get("fps", 30))
        return stage_routes.run(clip_id, tracklets, events_list, fps)

    elif job_type == "coverage":
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        analytics_safe = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))
        offense_direction = int(input_artifacts.get("offense_direction", 1))
        return stage_coverage.run(
            clip_id,
            tracklets,
            events_list,
            fps,
            analytics_safe=analytics_safe,
            offense_direction=offense_direction,
        )

    elif job_type == "oline":
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        analytics_safe = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))
        return stage_oline.run(
            clip_id, tracklets, events_list, analytics_safe, fps, job_id
        )

    elif job_type == "pressure":
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        analytics_safe = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))
        return stage_pressure.run(
            clip_id,
            tracklets,
            events_list,
            analytics_safe,
            fps,
            job_id,
            offense_direction=int(input_artifacts.get("offense_direction", 1)),
            down=input_artifacts.get("down"),
            distance_yards=input_artifacts.get("distance_yards"),
        )

    elif job_type == "self_scout":
        from pipeline import backend as backend_mod

        video_id_for_scout = input_artifacts.get("video_id")
        clips, labels_by_clip = backend_mod.fetch_clips_with_labels(
            video_id=video_id_for_scout,
        )
        metrics_by_clip: dict[str, list[dict[str, Any]]] = {}  # populated downstream
        return stage_self_scout.run(clips, labels_by_clip, metrics_by_clip)

    elif job_type == "pose":
        from pipeline import stage_pose, video_ingest

        model_path = os.environ.get("MODEL_POSE_PATH")
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        analytics_safe = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))

        # ``open_video`` silently falls back to ``MockVideoSource`` when the
        # URI is empty.  That is desirable in unit tests, but in a deployed
        # worker an empty ``input_uri`` for a pose job almost certainly means
        # the dispatcher dropped the artifact — log loudly so we don't silently
        # produce metrics from synthetic frames.
        if not input_uri:
            log.warning(
                "stage_pose_input_uri_missing_using_mock_source",
                job_id=job_id,
                clip_id=clip_id,
            )

        with video_ingest.open_video(input_uri or None, fps_fallback=fps) as source:
            return stage_pose.run(
                clip_id,
                source,
                tracklets,
                events_list,
                analytics_safe,
                fps,
                job_id,
                model_path,
            )

    elif job_type == "embeddings":
        # Nightly-only by design (issue #76 + docs/embeddings-architecture.md
        # §11). The variant ``play-embed-clip-vitb32-baseline`` is the only
        # one in NIGHTLY_ONLY_VARIANTS for ``embeddings`` so the routing
        # safety guard already prevents same-session execution.
        from pipeline import backend as backend_mod
        from pipeline import stage_embed

        variant = model_router.select_model("embeddings", priority)
        if variant == "none":
            log.info("stage_embed_skipped_no_variant", clip_id=clip_id)
            return {"embedding_skipped": True}
        if model_router.is_same_session(priority):
            log.warning(
                "stage_embed_rejected_same_session",
                clip_id=clip_id,
                priority=priority,
            )
            return {"embedding_skipped": True, "reason": "same_session_blocked"}

        model_version_id = input_artifacts.get("model_version_id")
        if not model_version_id:
            log.warning("stage_embed_missing_model_version_id", clip_id=clip_id)
            return {"embedding_skipped": True, "reason": "missing_model_version_id"}

        result = stage_embed.run(
            clip_id=clip_id,
            clip=input_artifacts.get("clip", {}),
            tracklets=input_artifacts.get("tracklets", []),
            track_points=input_artifacts.get("track_points", []),
            pose_keypoints=input_artifacts.get("pose_keypoints", []),
            labels=input_artifacts.get("labels", []),
            events=input_artifacts.get("events", []),
            fps=float(input_artifacts.get("fps", 60.0)),
            sam_masks=input_artifacts.get("sam_masks"),
        )
        backend_mod.create_play_embedding(
            clip_id=clip_id,
            model_version_id=str(model_version_id),
            vector=result.vector,
            visual_vector=result.visual_vector,
            structured_vector=result.structured_vector,
            clip_vector=result.clip_vector,
            chunk_kind=result.chunk_kind,
            snap_anchor=result.snap_anchor,
            used_sam_masks=result.used_sam_masks,
            embedding_confidence=result.embedding_confidence,
            source_label_ids=result.source_label_ids,
            calibration_version_id=input_artifacts.get("calibration_version_id"),
            is_experimental=True,
            job_id=job_id,
        )
        return {
            "embedding_written": True,
            "snap_anchor": result.snap_anchor,
            "used_sam_masks": result.used_sam_masks,
            "embedding_confidence": result.embedding_confidence,
            "has_clip_vector": result.clip_vector is not None,
        }

    elif job_type == "render":
        from pipeline.lightweight_config import use_period_renderer
        from renderer import period_renderer

        tracklets = input_artifacts.get("tracklets", [])
        labels_list: list[dict[str, Any]] = input_artifacts.get("labels", [])
        metrics_list: list[dict[str, Any]] = input_artifacts.get("metrics", [])
        analytics_safe = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))
        video_path = r2_mod.download_to_temp(_uri_to_r2_key(input_uri))
        try:
            if use_period_renderer(priority):
                return period_renderer.run(
                    clip_id,
                    video_path,
                    tracklets,
                    labels_list,
                    analytics_safe,
                    fps,
                )
            return stage_render.run(
                clip_id,
                video_path,
                tracklets,
                labels_list,
                metrics_list,
                analytics_safe,
                fps,
                BACKEND_API_URL,
            )
        finally:
            video_path.unlink(missing_ok=True)

    elif job_type == "render_hls":
        from renderer import hls_encoder

        overlay_uri = input_artifacts.get("overlay_uri") or input_artifacts.get(
            "period_overlay_uri", ""
        )
        fps = float(input_artifacts.get("fps", 30))
        if not overlay_uri:
            log.warning("render_hls_missing_overlay_uri", job_id=job_id)
            return {"hls_skipped": True, "reason": "no_overlay_uri"}
        overlay_path = r2_mod.download_to_temp(_uri_to_r2_key(overlay_uri))
        try:
            return hls_encoder.run(clip_id, overlay_path, fps)
        finally:
            overlay_path.unlink(missing_ok=True)

    elif job_type == "train":
        # Scheduled retraining from exported coach corrections (learning
        # loop, P3). Training output registers as EXPERIMENTAL in the MLOps
        # registry — promotion to production stays a human decision, so a
        # scheduler-triggered run can never silently change serving behavior.
        import dataclasses

        from pipeline import backend as backend_mod
        from training.cross_regime_distill import run_nightly

        clips = backend_mod.fetch_clips_for_pairing()
        video_dir = os.environ.get("TRAINING_VIDEO_DIR", "") or None
        try:
            _checkpoint, report = run_nightly(clips, video_dir=video_dir)
        except ValueError as exc:
            # Not enough validated paired plays yet — expected early in a
            # team's life; the job succeeds with an explicit no-train reason.
            log.info("train_skipped_insufficient_data", reason=str(exc))
            return {"trained": False, "reason": str(exc)}
        report_dict = (
            dataclasses.asdict(report) if dataclasses.is_dataclass(report) else {"summary": str(report)}
        )
        return {"trained": True, "report": report_dict}

    else:
        log.warning("unknown_job_type", job_type=job_type, job_id=job_id)
        return {}


def _uri_to_r2_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


def _queue_nightly_hls_followup(
    original_job: dict[str, Any],
    render_artifacts: dict[str, Any],
) -> None:
    """Queue a nightly follow-up render_hls job after same-session render."""
    import uuid as _uuid
    from queue.same_session_queue import NIGHTLY_PRIORITY, push_nightly_job

    clip_id = original_job.get("clipId", "")
    video_id = original_job.get("videoId", "")
    overlay_uri = render_artifacts.get("period_overlay_uri") or render_artifacts.get(
        "overlay_uri", ""
    )
    followup_job_id = str(_uuid.uuid4())

    followup_payload: dict[str, Any] = {
        "jobId": followup_job_id,
        "jobType": "render_hls",
        "videoId": video_id,
        "clipId": clip_id,
        "inputUri": overlay_uri,
        "priority": NIGHTLY_PRIORITY,
        "inputArtifacts": {
            "overlay_uri": overlay_uri,
            "fps": render_artifacts.get("fps", 30),
            "pipeline_mode": "nightly",
            "_same_session_origin_job": original_job.get("jobId"),
        },
    }

    try:
        msg_id = push_nightly_job(followup_payload)
        log.info(
            "nightly_hls_followup_queued",
            followup_job_id=followup_job_id,
            clip_id=clip_id,
            message_id=msg_id,
        )
    except Exception as exc:
        log.error(
            "nightly_hls_followup_queue_failed",
            clip_id=clip_id,
            error=str(exc),
        )

    # Best-effort: create a backend job record for the follow-up.
    if BACKEND_API_URL:
        try:
            with httpx.Client(base_url=BACKEND_API_URL, timeout=10, headers=_auth_headers()) as c:
                c.post(
                    "/api/v1/jobs",
                    json={
                        "id": followup_job_id,
                        "video_id": video_id,
                        "job_type": "render_hls",
                        "priority": NIGHTLY_PRIORITY,
                        "pipeline_mode": "nightly",
                        "input_artifacts": followup_payload["inputArtifacts"],
                    },
                )
        except Exception as exc:
            log.warning(
                "nightly_hls_followup_backend_create_failed",
                followup_job_id=followup_job_id,
                error=str(exc),
            )


def _finalize_nightly_clips(video_id: str) -> None:
    """Flip a video's same-session preliminary clips to final (Issue #147)."""
    from pipeline import backend as backend_mod

    try:
        count = backend_mod.finalize_video_clips(video_id)
        if count:
            log.info("nightly_clips_finalized", video_id=video_id, finalized_count=count)
    except Exception as exc:
        log.warning("nightly_clips_finalize_failed", video_id=video_id, error=str(exc))


def _update_job_status(
    job_id: str,
    status: str,
    error_message: str | None = None,
    output_artifacts: dict[str, Any] | None = None,
) -> None:
    if not BACKEND_API_URL:
        return
    try:
        # worker_id identifies this process as the lease holder — the backend
        # rejects state changes on a leased job from anyone else.
        payload: dict[str, Any] = {"status": status, "worker_id": _worker_id()}
        if error_message:
            payload["error_message"] = error_message
        if output_artifacts:
            payload["output_artifacts"] = output_artifacts
        with httpx.Client(base_url=BACKEND_API_URL, timeout=10, headers=_auth_headers()) as c:
            resp = c.patch(f"/api/v1/jobs/{job_id}", json=payload)
            if resp.status_code == 401:
                from worker import auth as worker_auth

                worker_auth.invalidate()
                c.headers.update(_auth_headers())
                c.patch(f"/api/v1/jobs/{job_id}", json=payload)
    except Exception as exc:
        log.warning("status_update_failed", job_id=job_id, error=str(exc))


# ── Main loop ─────────────────────────────────────────────────────────────────


def _run_cf_loop() -> None:
    """Legacy loop: long-poll Cloudflare Queues, one stage per message."""
    from worker.observability import record_heartbeat, record_queue_poll

    with httpx.Client() as client:
        while not _shutdown:
            try:
                messages = pull_messages(client)
                record_queue_poll("success", len(messages))
                record_heartbeat()
                for msg in messages:
                    if _shutdown:
                        break
                    body: dict[str, Any] = msg.get("body", {})
                    lease_id: str = msg.get("lease_id", "")
                    process_job(body)
                    if lease_id:
                        ack_message(client, lease_id)

            except httpx.HTTPStatusError as exc:
                record_queue_poll("error")
                log.error(
                    "queue_pull_error", status=exc.response.status_code, error=str(exc)
                )
            except Exception as exc:
                record_queue_poll("error")
                log.error("worker_loop_error", error=str(exc))

            if not _shutdown:
                record_heartbeat()
                time.sleep(POLL_INTERVAL)


def _run_db_loop() -> None:
    """Default loop: claim jobs from the backend's processing_jobs table."""
    import random

    from worker.observability import record_heartbeat, record_queue_poll

    if not BACKEND_API_URL:
        raise SystemExit(
            "QUEUE_BACKEND=db requires BACKEND_API_URL (set QUEUE_BACKEND=cf for the legacy queue)"
        )

    with httpx.Client() as client:
        while not _shutdown:
            claimed = None
            try:
                claimed = claim_db_job(client)
                record_queue_poll("success", 1 if claimed else 0)
                record_heartbeat()
            except Exception as exc:
                record_queue_poll("error")
                log.error("db_claim_error", error=str(exc))

            if claimed is not None:
                job = _db_row_to_job(claimed)
                try:
                    process_job(job)
                except Exception as exc:
                    # process_job handles its own failures; this is a belt-and-
                    # braces guard so one bad job never kills the loop.
                    log.error("job_processing_crashed", job_id=job.get("jobId"), error=str(exc))
                continue  # drain the queue before sleeping again

            if not _shutdown:
                record_heartbeat()
                # ±20% jitter so a worker fleet doesn't thundering-herd claims.
                time.sleep(POLL_INTERVAL * random.uniform(0.8, 1.2))


def main() -> None:
    from worker.observability import record_heartbeat, set_worker_up, start_metrics_server

    metrics_port = int(os.environ.get("GPU_METRICS_PORT", "9090"))
    try:
        start_metrics_server(port=metrics_port)
        log.info("metrics_server_started", port=metrics_port)
    except Exception as exc:
        log.warning("metrics_server_start_failed", port=metrics_port, error=str(exc))

    log.info(
        "gpu_worker_starting",
        queue_backend=QUEUE_BACKEND,
        worker_id=_worker_id(),
        poll_interval=POLL_INTERVAL,
    )
    set_worker_up(True)
    record_heartbeat()

    if QUEUE_BACKEND == "cf":
        _run_cf_loop()
    else:
        _run_db_loop()

    set_worker_up(False)
    log.info("gpu_worker_stopped")


if __name__ == "__main__":
    main()
