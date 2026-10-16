"""GPU Worker — claims processing jobs and runs the video pipeline.

The backend's ``processing_jobs`` table IS the queue. The worker claims
runnable rows via ``POST /api/v1/jobs/claim`` (FOR UPDATE SKIP LOCKED
server-side), heartbeats its lease, and runs ``pipeline`` jobs through
:mod:`pipeline.orchestrator` with per-stage progress reported into the job row.

Environment variables:
  BACKEND_API_URL           — backend base URL (required)
  WORKER_EMAIL / WORKER_PASSWORD — seeded service-account credentials
  WORKER_ID                 — claim identity (default: hostname-pid)
  GPU_WORKER_POLL_INTERVAL  — seconds between queue polls (default: 2)
  STORAGE_BACKEND / LOCAL_STORAGE_ROOT — see pipeline.storage
  S3_* / OBJECT_STORE_*     — object-store credentials (s3 backend only)
  MODEL_DETECT_PATH         — path to YOLO weights (default: yolov8n.pt)
  MODEL_POSE_PATH           — optional real pose weights (absent means skipped)
  PIPELINE_DEVICE           — cpu by default; cuda/mps require explicit opt-in
  PIPELINE_CHECKPOINT_ROOT  — durable local stage ledger (default data/checkpoints)
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
POLL_INTERVAL = max(1, min(60, int(os.environ.get("GPU_WORKER_POLL_INTERVAL", "2"))))
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "")
LEASE_SECONDS = max(30, min(3600, int(os.environ.get("GPU_WORKER_LEASE_SECONDS", "120"))))
HEARTBEAT_INTERVAL = max(
    1, min(5, LEASE_SECONDS // 3, int(os.environ.get("GPU_WORKER_HEARTBEAT_INTERVAL", "5")))
)
_CURRENT_LEASES: dict[str, dict[str, Any]] = {}


def _worker_id() -> str:
    from worker.auth import worker_id

    return worker_id()


# ── Graceful shutdown ─────────────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    log.info("shutdown_signal_received", signal=signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _shutdown_requested() -> bool:
    from pathlib import Path

    stop_file = os.environ.get("LOCAL_APP_STOP_FILE")
    return _shutdown or bool(stop_file and Path(stop_file).is_file())


def _idle_wait(seconds: float) -> None:
    import threading

    deadline = time.monotonic() + seconds
    tick = threading.Event()
    while not _shutdown_requested() and time.monotonic() < deadline:
        tick.wait(min(0.25, max(0, deadline - time.monotonic())))


if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _handle_signal)


# ── DB queue ──────────────────────────────────────────────────────────────────


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
        json=_claim_payload(),
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
            json=_claim_payload(),
            timeout=30,
        )
        if resp.status_code == 204:
            return None
    resp.raise_for_status()
    return dict(resp.json())


def _claim_payload() -> dict[str, Any]:
    capabilities = worker_capabilities()
    return {
        "worker_id": _worker_id(),
        "lease_seconds": LEASE_SECONDS,
        "job_types": capabilities["job_types"],
        "capabilities": {
            "compatible_devices": [capabilities["device"]],
            "model_names": [
                key for key, value in capabilities["models"].items() if value.get("ready")
            ],
            "profiles": capabilities["profiles"],
        },
    }


def heartbeat_db_job(
    job_id: str,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Extend the fenced lease and return pause/cancel instructions."""
    payload = {
        "worker_id": _worker_id(),
        "lease_seconds": LEASE_SECONDS,
        "progress": progress or {},
        **_CURRENT_LEASES.get(job_id, {}),
    }
    for attempt in range(2):
        try:
            resp = httpx.post(
                f"{BACKEND_API_URL}/api/v1/jobs/{job_id}/heartbeat",
                headers=_auth_headers(),
                json=payload,
                timeout=10,
            )
            if resp.status_code == 401 and attempt == 0:
                from worker import auth as worker_auth

                worker_auth.invalidate()
                continue
            if resp.status_code == 409:
                return {"control_requested": "lease_lost"}
            resp.raise_for_status()
            return dict(resp.json())
        except Exception as exc:
            log.warning("heartbeat_failed", job_id=job_id, error=str(exc))
            return None
    return None


def worker_capabilities() -> dict[str, Any]:
    """Advertise installed local resources without loading heavy models."""
    import importlib.util
    from pathlib import Path

    device = os.environ.get("PIPELINE_DEVICE", "cpu").strip().lower()
    if device not in {"cpu", "cuda", "mps"}:
        device = "cpu"
    models: dict[str, Any] = {}
    hosted_metadata: dict[str, Any] = {}
    if os.environ.get("PIPELINE_HOSTED") == "1":
        from pipeline.hosted_limits import processing_limits

        hosted_metadata["processing_limits"] = processing_limits()
    for name, variable, default, package in (
        ("detect", "MODEL_DETECT_PATH", "yolov8n.pt", "ultralytics"),
        ("pose", "MODEL_POSE_PATH", "", "torch"),
        ("ball", "MODEL_BALL_PATH", "", "ultralytics"),
    ):
        configured = os.environ.get(variable, default)
        path = Path(configured).expanduser() if configured else None
        ready = bool(path and path.is_file() and importlib.util.find_spec(package))
        models[name] = {"ready": ready, "path": str(path) if path else None}
    profiles = [
        profile
        for profile in os.environ.get("WORKER_PROFILES", "review,quick,detailed").split(",")
        if profile in {"review", "quick", "detailed"}
    ]
    job_types = (
        # Hosted media limits are enforced on the full pipeline path, and a
        # model-free Review worker cannot run standalone inference stages.
        ["pipeline"]
        if os.environ.get("PIPELINE_HOSTED") == "1" or profiles == ["review"]
        else list(_ALL_JOB_TYPES)
    )
    return {
        "worker_id": _worker_id(),
        "device": device,
        "profiles": profiles,
        "models": models,
        "job_types": job_types,
        "metadata": {
            **hosted_metadata,
            "storage_transport": os.environ.get("PIPELINE_STORAGE_TRANSPORT", "direct"),
            "checkpoint_root": os.environ.get("PIPELINE_CHECKPOINT_ROOT", "data/checkpoints"),
        },
    }


_ALL_JOB_TYPES: tuple[str, ...] = (
    "pipeline",
    "ingest",
    "segment",
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
    "self_scout",
    "embeddings",
    "workload_rollup",
    "train",
)


def register_worker(client: httpx.Client) -> None:
    try:
        response = client.post(
            f"{BACKEND_API_URL}/api/v1/system/workers/register",
            headers=_auth_headers(),
            json=worker_capabilities(),
            timeout=10,
        )
        if response.status_code != 404:
            response.raise_for_status()
    except Exception as exc:
        log.warning("worker_registration_failed", error=str(exc))


def heartbeat_worker(client: httpx.Client) -> None:
    try:
        response = client.post(
            f"{BACKEND_API_URL}/api/v1/system/workers/{_worker_id()}/heartbeat",
            headers=_auth_headers(),
            json=worker_capabilities(),
            timeout=10,
        )
        if response.status_code == 404:
            # The initial registration can race API boot/recovery. A later
            # heartbeat must recreate the row instead of staying invisible.
            register_worker(client)
            return
        if response.status_code != 404:
            response.raise_for_status()
    except Exception as exc:
        log.warning("worker_capability_heartbeat_failed", error=str(exc))


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
    """Map a claimed processing_jobs row onto the job-dict shape stages expect."""
    input_artifacts = dict(row.get("input_artifacts") or {})
    nested = input_artifacts.pop("inputArtifacts", None) or input_artifacts.pop(
        "input_artifacts", None
    )
    if isinstance(nested, dict):
        input_artifacts.update(nested)
    # job_dispatch-created rows carry the camelCase payload in
    # input_artifacts; backend-created rows may use snake_case.
    input_uri = str(input_artifacts.get("input_uri") or input_artifacts.get("inputUri") or "")
    if row.get("training_dataset_id"):
        input_artifacts.setdefault("training_dataset_id", row["training_dataset_id"])
    video_id = str(row.get("video_id") or "")
    if not input_uri and video_id:
        video = _fetch_video(video_id)
        if video:
            input_uri = str(video.get("storage_uri") or "")
    return {
        "jobId": str(row.get("id", "")),
        "jobType": str(row.get("job_type", "")),
        "videoId": video_id,
        "clipId": str(row.get("clip_id") or input_artifacts.get("clipId") or ""),
        "inputUri": input_uri,
        "priority": int(row.get("priority") or 0),
        "inputArtifacts": input_artifacts,
        "pipelineMode": row.get("pipeline_mode"),
        "leaseToken": row.get("lease_token"),
        "attemptCount": row.get("attempt_count"),
        "modelVersionId": row.get("model_version_id"),
        "controlRequested": row.get("control_requested"),
    }


def process_pipeline_job(job: dict[str, Any]) -> None:
    """Run a local profile with bounded heartbeats and cooperative controls."""
    import threading
    from pathlib import Path

    from pipeline import backend as backend_client
    from pipeline.hosted_limits import HostedCapacityError, release_completed_checkpoints
    from pipeline.orchestrator import DirArtifactSink, run_pipeline
    from pipeline.runtime import PipelineInterrupted, job_context
    from worker.observability import record_job_failed, record_job_started, record_job_succeeded

    job_id, video_id, input_uri = job["jobId"], job["videoId"], job["inputUri"]
    priority = int(job.get("priority", 0))
    pipeline_mode = job.get("pipelineMode") or ("same_session" if priority >= 10 else "nightly")
    inputs = dict(job.get("inputArtifacts") or {})
    profile = inputs.get("processing_profile") or os.environ.get("PROCESSING_PROFILE", "quick")
    if not input_uri:
        _update_job_status(job_id, "failed", error_message="no input_uri resolvable")
        return

    fence = {
        key: value
        for key, value in {
            "lease_token": job.get("leaseToken"),
            "attempt_count": job.get("attemptCount"),
        }.items()
        if value is not None
    }
    _CURRENT_LEASES[job_id] = fence
    record_job_started("pipeline", pipeline_mode)
    started = time.monotonic()
    state: dict[str, Any] = {}
    state_lock = threading.Lock()
    stop = threading.Event()
    control: dict[str, Any] = {
        "action": job.get("controlRequested"),
        "last_heartbeat": started,
        "last_capability_heartbeat": started,
    }

    def beat() -> None:
        with state_lock:
            snapshot = dict(state)
        reply = heartbeat_db_job(job_id, snapshot)
        if isinstance(reply, dict):
            control["last_heartbeat"] = time.monotonic()
            action = reply.get("control_requested")
            if action:
                control["action"] = action
            elif reply.get("status") in {"cancelled", "paused"}:
                control["action"] = "cancel" if reply["status"] == "cancelled" else "pause"
        elif time.monotonic() - control["last_heartbeat"] >= LEASE_SECONDS * 0.75:
            # Stop before a lost lease can publish more output. A reclaimed run
            # reads the same durable compatible ledger.
            control["action"] = "lease_lost"

    def progress_cb(stage: str, clip_id: str | None, status: str, extra: dict[str, Any]) -> None:
        # Use bounded keys: the backend merges progress and caps JSON at
        # 64 KiB, so per-clip UUID keys cannot accumulate through a full game.
        key = "current_clip" if clip_id else stage
        with state_lock:
            completed = int(state.get("completed_clip_stages", {}).get("count", 0))
            if clip_id and status in {"succeeded", "skipped", "failed"}:
                state["completed_clip_stages"] = {"count": completed + 1}
            state[key] = {"stage": stage, "clip_id": clip_id, "status": status, **extra}
        beat()

    def heartbeat_loop() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL):
            beat()
            if time.monotonic() - control["last_capability_heartbeat"] >= 20:
                with httpx.Client() as client:
                    heartbeat_worker(client)
                control["last_capability_heartbeat"] = time.monotonic()

    def control_cb() -> str | None:
        return control["action"] or ("shutdown" if _shutdown_requested() else None)

    thread = threading.Thread(target=heartbeat_loop, name="job-heartbeat", daemon=True)
    thread.start()
    checkpoint_root = Path(os.environ.get("PIPELINE_CHECKPOINT_ROOT", "data/checkpoints"))
    # IDs from the API are UUIDs, but keep directory ownership safe for tests
    # and legacy jobs as well.
    import hashlib

    video_key = hashlib.sha256(video_id.encode()).hexdigest()
    sink = DirArtifactSink(checkpoint_root / video_key)
    revisions = {
        key: inputs[key]
        for key in (
            "calibration_revision",
            "correction_revision",
            "annotation_revision",
            "model_revision",
            "config_revision",
            "source_revision",
        )
        if key in inputs
    }
    if job.get("modelVersionId"):
        revisions["model_version_id"] = job["modelVersionId"]
    try:
        with job_context(
            control=control_cb, lease={"job_id": job_id, "worker_id": _worker_id(), **fence}
        ):
            video_context = backend_client.fetch_video_context(video_id)
            existing_clips = backend_client.fetch_annotation_snapshot(video_id)
            revisions["effective_annotations"] = [
                clip for clip in existing_clips if (clip.get("annotation_revision") or 0) > 0
            ]
            revisions["learning_splits"] = [
                {"id": clip["id"], "split": (clip.get("label_data") or {}).get("_learning_split")}
                for clip in existing_clips
                if (clip.get("label_data") or {}).get("_learning_split")
            ]
            result = run_pipeline(
                video_id,
                input_uri,
                priority=priority,
                job_id=job_id,
                progress_cb=progress_cb,
                artifact_sink=sink,
                roster=backend_client.fetch_roster_for_video(video_id),
                processing_profile=profile,
                revisions=revisions,
                lineage={
                    "existing_clips": existing_clips,
                    "session_id": video_context.get("practice_session_id"),
                    "recording_id": video_id,
                    **{
                        key: inputs[key]
                        for key in ("session_id", "recording_id", "split")
                        if key in inputs
                    },
                },
            )
            result = _bounded_job_summary(
                result, job_id, publish=not bool(result.get("interrupted_action"))
            )
        stop.set()
        action = result.get("interrupted_action")
        if action in {"pause", "shutdown"}:
            if action == "shutdown":
                result["interrupted_worker_id"] = _worker_id()
            accepted = _update_job_status(job_id, "paused", output_artifacts=result)
            if accepted is not False:
                backend_client.patch_video_status(
                    video_id, "ready" if result.get("playback_ready") else "uploaded"
                )
        elif action == "cancel":
            accepted = _update_job_status(job_id, "cancelled", output_artifacts=result)
            if accepted is not False:
                backend_client.patch_video_status(
                    video_id, "ready" if result.get("playback_ready") else "uploaded"
                )
        elif action == "lease_lost":
            log.warning("job_lease_lost", job_id=job_id)
        elif result.get("result_state") == "partial":
            failures = result.get("failed_video_stages", []) + result.get("failed_clip_stages", [])
            reason = "; ".join(f"{item['stage']}: {item['error']}" for item in failures)
            accepted = _update_job_status(
                job_id, "failed", error_message=reason, output_artifacts=result
            )
            if accepted is False:
                return
            # Imported film remains reviewable after an inference failure.
            backend_client.patch_video_status(
                video_id, "ready" if result.get("playback_ready") else "failed"
            )
            record_job_failed("pipeline", pipeline_mode)
        else:
            accepted = _update_job_status(job_id, "succeeded", output_artifacts=result)
            if accepted is False:
                return
            release_completed_checkpoints(sink.root)
            backend_client.patch_video_status(
                video_id, "ready" if result.get("playback_ready", True) else "uploaded"
            )
            record_job_succeeded("pipeline", pipeline_mode, time.monotonic() - started)
    except HostedCapacityError as exc:
        accepted = _update_job_status(job_id, "failed", error_message=str(exc))
        if accepted is not False:
            backend_client.patch_video_status(video_id, "uploaded")
    except PipelineInterrupted as exc:
        if exc.action != "lease_lost":
            interrupted = {
                "interrupted_action": exc.action,
                "interrupted_worker_id": _worker_id(),
            }
            _update_job_status(
                job_id,
                "cancelled" if exc.action == "cancel" else "paused",
                output_artifacts=interrupted,
            )
    except Exception as exc:
        record_job_failed("pipeline", pipeline_mode)
        log.error("pipeline_job_failed", job_id=job_id, error=str(exc))
        accepted = _update_job_status(job_id, "failed", error_message=str(exc))
        if accepted is not False:
            backend_client.patch_video_status(video_id, "failed")
    finally:
        stop.set()
        thread.join(timeout=12)
        _CURRENT_LEASES.pop(job_id, None)


def _bounded_job_summary(
    result: dict[str, Any], job_id: str, *, publish: bool = True
) -> dict[str, Any]:
    """Keep database rows small while preserving full evidence as an artifact."""
    import json

    from pipeline import storage

    serialized = json.dumps(result, default=str).encode()
    if len(serialized) <= 48 * 1024:
        return result
    fingerprint = (result.get("checkpoint") or {}).get("fingerprint", "summary")
    uri = (
        storage.upload_bytes(
            serialized, f"pipeline_summaries/{job_id}/{fingerprint}.json", "application/json"
        )
        if publish
        else None
    )
    compact = {
        key: value
        for key, value in result.items()
        if key not in {"stage_states", "stage_timings", "clips"}
    }
    if uri:
        compact["summary_artifact_uri"] = uri
    compact["checkpoint"] = {
        key: value
        for key, value in (result.get("checkpoint") or {}).items()
        if key in {"fingerprint", "source_sha256", "processing_profile"}
    }
    compact["clips"] = [
        {
            key: value
            for key, value in clip.items()
            if key not in {"proxy_timeline", "overlay_proxy_timeline"}
        }
        for clip in result.get("clips", [])[:50]
    ]
    compact["clips_truncated"] = len(result.get("clips", [])) > 50
    for key in ("failed_video_stages", "failed_clip_stages"):
        failures = result.get(key, [])
        compact[f"{key}_count"] = len(failures)
        compact[key] = [
            {**failure, "error": str(failure.get("error", ""))[:1000]} for failure in failures[:20]
        ]
    # Long object URIs and many independent proxy failures can still exceed
    # the limit; previews shrink while counts and the full artifact remain.
    while len(json.dumps(compact, default=str).encode()) > 48 * 1024 and compact["clips"]:
        compact["clips"] = compact["clips"][: len(compact["clips"]) // 2]
        compact["clips_truncated"] = True
    for key in ("failed_video_stages", "failed_clip_stages"):
        while len(json.dumps(compact, default=str).encode()) > 48 * 1024 and len(compact[key]) > 1:
            compact[key] = compact[key][: len(compact[key]) // 2]
    return compact


# ── Job processing ────────────────────────────────────────────────────────────


def process_job(job: dict[str, Any]) -> None:
    """Keep standalone stage jobs fenced and alive just like full pipelines."""
    if _reject_hosted_capacity(job):
        return
    if job.get("jobType") == "pipeline":
        process_pipeline_job(job)
        return
    import threading

    from pipeline.runtime import PipelineInterrupted, job_context

    job_id = str(job.get("jobId", ""))
    fence = {
        key: value
        for key, value in {
            "lease_token": job.get("leaseToken"),
            "attempt_count": job.get("attemptCount"),
        }.items()
        if value is not None
    }
    _CURRENT_LEASES[job_id] = fence
    stop = threading.Event()
    state: dict[str, Any] = {"action": job.get("controlRequested"), "heartbeat": time.monotonic()}

    def beat_loop() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL):
            reply = heartbeat_db_job(job_id)
            if isinstance(reply, dict):
                state["heartbeat"] = time.monotonic()
                if reply.get("control_requested"):
                    state["action"] = reply["control_requested"]
            elif time.monotonic() - state["heartbeat"] >= LEASE_SECONDS * 0.75:
                state["action"] = "lease_lost"

    thread = threading.Thread(target=beat_loop, name="stage-heartbeat", daemon=True)
    thread.start()
    try:
        with job_context(
            control=lambda: state["action"] or ("shutdown" if _shutdown_requested() else None),
            lease={"job_id": job_id, "worker_id": _worker_id(), **fence},
        ):
            _process_single_stage_job(job)
    except PipelineInterrupted as exc:
        if exc.action != "lease_lost":
            _update_job_status(
                job_id,
                "cancelled" if exc.action == "cancel" else "paused",
                output_artifacts={
                    "interrupted_action": exc.action,
                    "interrupted_worker_id": _worker_id(),
                },
            )
    finally:
        stop.set()
        thread.join(timeout=12)
        _CURRENT_LEASES.pop(job_id, None)


def _reject_hosted_capacity(job: dict[str, Any]) -> bool:
    """Check verified object size before downloading to the shared API disk."""
    if os.environ.get("PIPELINE_HOSTED") != "1":
        return False
    from pipeline.hosted_limits import check_source

    reason: str | None
    if job.get("jobType") != "pipeline":
        # Standalone stages bypass the pipeline's duration/resolution probe.
        reason = "Hosted CPU processing runs full pipeline jobs only."
    elif not job.get("videoId"):
        reason = "Hosted CPU processing requires a registered recording."
    else:
        video = _fetch_video(str(job["videoId"])) or {}
        storage_uri = str(video.get("storage_uri") or "")
        if not storage_uri or job.get("inputUri") != storage_uri:
            # The verified size belongs to the recording's own object only.
            reason = "Hosted CPU processing only reads the registered recording's verified object."
        else:
            reason = check_source(video.get("size_bytes"))
    if reason is None:
        return False
    job_id = str(job.get("jobId", ""))
    _CURRENT_LEASES[job_id] = {
        key: value
        for key, value in {
            "lease_token": job.get("leaseToken"),
            "attempt_count": job.get("attemptCount"),
        }.items()
        if value is not None
    }
    try:
        _update_job_status(job_id, "failed", error_message=reason)
    finally:
        _CURRENT_LEASES.pop(job_id, None)
    return True


def _process_single_stage_job(job: dict[str, Any]) -> None:
    """Dispatch a single job to the appropriate processing stage.

    Same-session jobs (priority >= SAME_SESSION_PRIORITY) are wrapped in the
    8-minute timeout handler.  If the job exceeds the deadline it is requeued
    for nightly processing and the backend job record is marked failed.
    """
    from pipeline import model_router
    from pipeline.lightweight_config import should_skip_stage
    from pipeline.runtime import PipelineInterrupted, check_control
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

    check_control()
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
        check_control()
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

    except PipelineInterrupted:
        raise
    except JobTimeoutError:
        _jto(job_type, pipeline_mode)
        log.warning("job_timeout_handled", job_id=job_id, priority=priority)
    except Exception as exc:
        _jf(job_type, pipeline_mode)
        log.error("job_failed", job_id=job_id, error=str(exc))
        _update_job_status(job_id, "failed", error_message=str(exc))


def _offense_direction_artifact(input_artifacts: dict[str, Any]) -> int | None:
    """Read the attacking direction from job artifacts, or ``None``.

    Only ``+1`` and ``-1`` mean anything; everything else -- absent, null, a
    stray string from a hand-built job -- is "not resolved". This used to
    default to ``+1``, which silently supplied a sign that flips every
    depth-behind-LOS feature when it is wrong.
    """
    value = input_artifacts.get("offense_direction")
    if value in (1, -1, "1", "-1", "+1"):
        return int(value)
    return None


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
        object_store as object_store_mod,
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
        video_path = object_store_mod.download_to_temp(_uri_to_object_key(input_uri))
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
        # Nightly per-player workload rollup (Issue #149). Enqueued by the
        # backend scheduler; reads its inputs from the backend API, so there
        # is no input_uri. Defaults to yesterday (UTC) when the job payload
        # somehow lacks a date.
        from datetime import UTC, datetime, timedelta

        from pipeline import stage_workload_rollup

        # The scheduler coalesces days missed while no worker was attached
        # into one job's ``dates`` list; replay them oldest first so each
        # day's trailing window sees the rows written for the days before it.
        rollup_dates = sorted(
            {str(d)[:10] for d in input_artifacts.get("dates") or []}
            | ({str(input_artifacts["date"])[:10]} if input_artifacts.get("date") else set())
        ) or [(datetime.now(UTC).date() - timedelta(days=1)).isoformat()]
        results = [
            stage_workload_rollup.run(day, job_id, priority=priority) for day in rollup_dates
        ]
        if len(results) == 1:
            return results[0]
        return {"dates": rollup_dates, "results": results}

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
        return stage_coverage.run(
            clip_id,
            tracklets,
            events_list,
            fps,
            analytics_safe=analytics_safe,
            offense_direction=_offense_direction_artifact(input_artifacts),
        )

    elif job_type == "oline":
        tracklets = input_artifacts.get("tracklets", [])
        events_list = input_artifacts.get("events", [])
        analytics_safe = bool(input_artifacts.get("analytics_safe", False))
        fps = float(input_artifacts.get("fps", 30))
        return stage_oline.run(clip_id, tracklets, events_list, analytics_safe, fps, job_id)

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
            offense_direction=_offense_direction_artifact(input_artifacts),
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
        video_path = object_store_mod.download_to_temp(_uri_to_object_key(input_uri))
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
        overlay_path = object_store_mod.download_to_temp(_uri_to_object_key(overlay_uri))
        try:
            return hls_encoder.run(clip_id, overlay_path, fps)
        finally:
            overlay_path.unlink(missing_ok=True)

    elif job_type == "train":
        if input_artifacts.get("training_variant") != "local_coverage":
            raise RuntimeError(
                "Training requires the supported local_coverage variant and actual "
                "reviewed labels/features artifacts; automatic pseudo-label training is unavailable."
            )
        return _train_local_coverage(input_artifacts, job_id)

    else:
        log.warning("unknown_job_type", job_type=job_type, job_id=job_id)
        raise ValueError(f"Unsupported job type: {job_type}")


def _train_local_coverage(inputs: dict[str, Any], job_id: str) -> dict[str, Any]:
    import hashlib
    import json
    import tempfile
    import uuid
    from pathlib import Path

    from pipeline import backend, storage
    from pipeline.runtime import check_control
    from training.local_coverage import read_jsonl, train_candidate

    labels_uri, features_uri = inputs.get("labels_uri"), inputs.get("features_uri")
    if not labels_uri or not features_uri:
        raise ValueError("Local coverage training requires labels_uri and features_uri")
    labels_path = storage.download_to_temp(str(labels_uri))
    features_path = None
    try:
        features_path = storage.download_to_temp(str(features_uri))
        labels, features = read_jsonl(labels_path), read_jsonl(features_path)
        check_control()
        with tempfile.TemporaryDirectory(prefix="football-iq-training-") as temporary:
            result = train_candidate(labels, features, Path(temporary))
            check_control()
            checkpoint_sha = hashlib.sha256(
                Path(result["checkpoint_path"]).read_bytes()
            ).hexdigest()
            root = f"models/local_coverage/{checkpoint_sha}"
            artifact_uri = storage.upload_file(
                Path(result["checkpoint_path"]), f"{root}/checkpoint.json"
            )
            report_uri = storage.upload_file(Path(result["report_path"]), f"{root}/evaluation.json")
            predictions_uri = storage.upload_file(
                Path(result["predictions_path"]), f"{root}/holdout_predictions.json"
            )
            metrics = {key: value for key, value in result.items() if not key.endswith("_path")}
            metrics.update(
                {
                    "report_uri": report_uri,
                    "predictions_uri": predictions_uri,
                    "labels_uri": str(labels_uri),
                    "features_uri": str(features_uri),
                    "training_variant": "local_coverage",
                    "artifact_sha256": checkpoint_sha,
                }
            )
            model = backend.register_model_version(
                model_name="local-coverage",
                version=checkpoint_sha[:16],
                model_type="coverage",
                artifact_uri=artifact_uri,
                metrics=metrics,
                training_dataset_id=inputs.get("training_dataset_id"),
                model_version_id=str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"local-coverage:{checkpoint_sha}")
                ),
            )
            if backend.BACKEND_API_URL and model is None:
                raise RuntimeError(
                    "Candidate artifacts saved but model registry publication failed"
                )
            prediction_label_ids = []
            if model is not None:
                for prediction in json.loads(Path(result["predictions_path"]).read_text()):
                    check_control()
                    key = (
                        f"candidate-prediction:{model['id']}:"
                        f"{prediction['clip_id']}:{prediction['label_type']}"
                    )
                    label = backend.create_label(
                        prediction["label_type"],
                        prediction["label_value"],
                        clip_id=prediction["clip_id"],
                        source="model",
                        model_version_id=model["id"],
                        label_id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
                    )
                    prediction_label_ids.append(label["id"])
            return {
                "trained": True,
                "promoted": False,
                "model_version_id": (model or {}).get("id"),
                "artifact_uri": artifact_uri,
                "report_uri": report_uri,
                "predictions_uri": predictions_uri,
                "prediction_label_ids": prediction_label_ids,
                **metrics,
            }
    finally:
        labels_path.unlink(missing_ok=True)
        if features_path is not None:
            features_path.unlink(missing_ok=True)


def _uri_to_object_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


def _queue_nightly_hls_followup(
    original_job: dict[str, Any],
    render_artifacts: dict[str, Any],
) -> None:
    """Queue a nightly follow-up render_hls job after same-session render."""
    import uuid as _uuid
    from queue.same_session_queue import NIGHTLY_PRIORITY

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

    # The processing_jobs row IS the queue entry — inserting it is what makes
    # the follow-up claimable by a worker on the nightly pass.
    if not BACKEND_API_URL:
        log.warning("nightly_hls_followup_skipped_no_backend", clip_id=clip_id)
        return

    try:
        with httpx.Client(base_url=BACKEND_API_URL, timeout=10, headers=_auth_headers()) as c:
            resp = c.post(
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
            resp.raise_for_status()
        log.info(
            "nightly_hls_followup_queued",
            followup_job_id=followup_job_id,
            clip_id=clip_id,
        )
    except Exception as exc:
        log.error(
            "nightly_hls_followup_queue_failed",
            followup_job_id=followup_job_id,
            clip_id=clip_id,
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
) -> bool:
    if not BACKEND_API_URL:
        return True
    try:
        # worker_id identifies this process as the lease holder — the backend
        # rejects state changes on a leased job from anyone else.
        payload: dict[str, Any] = {
            "status": status,
            "worker_id": _worker_id(),
            **_CURRENT_LEASES.get(job_id, {}),
        }
        if error_message:
            payload["error_message"] = error_message
        if output_artifacts:
            payload["output_artifacts"] = output_artifacts
            payload["provenance"] = {
                "source": output_artifacts.get("source", "unvalidated"),
                "validated": bool(output_artifacts.get("trusted_analysis", False)),
                "model_provenance": output_artifacts.get("model_provenance", {}),
                "checkpoint_fingerprint": (output_artifacts.get("checkpoint") or {}).get(
                    "fingerprint"
                ),
            }
        with httpx.Client(base_url=BACKEND_API_URL, timeout=10, headers=_auth_headers()) as c:
            resp = c.patch(f"/api/v1/jobs/{job_id}", json=payload)
            if resp.status_code == 401:
                from worker import auth as worker_auth

                worker_auth.invalidate()
                c.headers.update(_auth_headers())
                resp = c.patch(f"/api/v1/jobs/{job_id}", json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("status_update_failed", job_id=job_id, error=str(exc))
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────


def _run_db_loop() -> None:
    """Claim jobs from the backend's processing_jobs table and run them."""
    import random

    from worker.keep_awake import keep_awake
    from worker.observability import record_heartbeat, record_queue_poll

    if not BACKEND_API_URL:
        raise SystemExit("BACKEND_API_URL is required — it is where jobs are claimed from.")

    with httpx.Client() as client:
        register_worker(client)
        last_registration = time.monotonic()
        while not _shutdown_requested():
            if time.monotonic() - last_registration >= 20:
                heartbeat_worker(client)
                last_registration = time.monotonic()
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
                    with keep_awake():
                        process_job(job)
                except Exception as exc:
                    # process_job handles its own failures; this is a belt-and-
                    # braces guard so one bad job never kills the loop.
                    log.error("job_processing_crashed", job_id=job.get("jobId"), error=str(exc))
                continue  # drain the queue before sleeping again

            if not _shutdown_requested():
                record_heartbeat()
                # ±20% jitter so a worker fleet doesn't thundering-herd claims.
                _idle_wait(POLL_INTERVAL * random.uniform(0.8, 1.2))


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
        worker_id=_worker_id(),
        poll_interval=POLL_INTERVAL,
    )
    set_worker_up(True)
    record_heartbeat()

    _run_db_loop()

    set_worker_up(False)
    log.info("gpu_worker_stopped")


if __name__ == "__main__":
    main()
