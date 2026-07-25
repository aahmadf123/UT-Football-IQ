"""Cloudflare Queue trigger — dispatches a job immediately on video upload.

When a video is uploaded (from a drone or iPad), this module:
  1. Determines whether the job is a same-session period-break (high priority)
     or a nightly full-session job (normal priority).
  2. Publishes the job payload to the appropriate Cloudflare Queue.
  3. Records the priority in the processing_jobs table via the backend API.

Priority decision rule:
  - If ``is_same_session=True`` the job is enqueued on the same-session queue
    with priority = SAME_SESSION_PRIORITY (10).
  - Otherwise it goes to the nightly queue with priority = NIGHTLY_PRIORITY (0).

Environment variables:
  CLOUDFLARE_ACCOUNT_ID   — Cloudflare account ID
  CLOUDFLARE_API_TOKEN    — Cloudflare API token with Queues write permission
  BACKEND_API_URL         — backend base URL for job status callbacks
"""

from __future__ import annotations

import os
import uuid
from queue.same_session_queue import (
    NIGHTLY_PRIORITY,
    SAME_SESSION_PRIORITY,
    push_nightly_job,
    push_same_session_job,
)
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "")


# ── Public API ────────────────────────────────────────────────────────────────


def dispatch_on_upload(
    video_id: str,
    storage_uri: str,
    job_type: str = "ingest",
    *,
    is_same_session: bool = False,
    clip_id: str | None = None,
    input_artifacts: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Dispatch a job to Cloudflare Queue immediately after a video upload.

    Args:
        video_id:       UUID of the video record.
        storage_uri:    R2 URI of the uploaded video (e.g. ``r2://football-iq/…``).
        job_type:       Pipeline stage to run (e.g. ``"ingest"``).
        is_same_session: If True the job targets the same-session high-priority queue.
        clip_id:        Optional clip UUID for clip-scoped jobs.
        input_artifacts: Optional dict of artifacts forwarded to the job.
        client:         Optional shared httpx.Client (created internally if absent).

    Returns:
        Dict with keys ``job_id``, ``message_id``, ``priority``, ``queue``.
    """
    job_id = str(uuid.uuid4())
    priority = SAME_SESSION_PRIORITY if is_same_session else NIGHTLY_PRIORITY

    pipeline_mode = "same_session" if is_same_session else "nightly"

    payload: dict[str, Any] = {
        "jobId": job_id,
        "jobType": job_type,
        "videoId": video_id,
        "inputUri": storage_uri,
        "priority": priority,
        "pipelineMode": pipeline_mode,
    }
    if clip_id:
        payload["clipId"] = clip_id
    if input_artifacts:
        payload["inputArtifacts"] = input_artifacts

    log.info(
        "cf_trigger_dispatch",
        job_id=job_id,
        video_id=video_id,
        job_type=job_type,
        is_same_session=is_same_session,
        priority=priority,
    )

    push_fn = push_same_session_job if is_same_session else push_nightly_job
    message_id = push_fn(payload, client=client)

    # Best-effort: create a processing_jobs record in the backend so the job
    # is visible in the UI immediately.
    _create_backend_job(
        job_id=job_id,
        video_id=video_id,
        job_type=job_type,
        priority=priority,
        input_artifacts=payload,
        client=client,
    )

    queue_name = (
        os.environ.get("CF_QUEUE_SAME_SESSION", "same-session-jobs")
        if is_same_session
        else os.environ.get("CF_QUEUE_VIDEO_PROCESSING", "video-processing-jobs")
    )

    return {
        "job_id": job_id,
        "message_id": message_id,
        "priority": priority,
        "queue": queue_name,
    }


# ── Backend job record ────────────────────────────────────────────────────────


def _create_backend_job(
    job_id: str,
    video_id: str,
    job_type: str,
    priority: int,
    input_artifacts: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> None:
    """POST /api/v1/jobs to create a tracking record for the dispatched job.

    Failures are logged and swallowed — the queue dispatch already happened.
    """
    if not BACKEND_API_URL:
        return

    payload: dict[str, Any] = {
        "id": job_id,
        "video_id": video_id,
        "job_type": job_type,
        "priority": priority,
        "input_artifacts": input_artifacts,
    }
    jobs_url = f"{BACKEND_API_URL.rstrip('/')}/api/v1/jobs"

    headers: dict[str, str] = {}
    try:
        from worker import auth as worker_auth

        bearer = worker_auth.token()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
    except ImportError:
        pass

    def _do(c: httpx.Client) -> None:
        try:
            resp = c.post(jobs_url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            log.info("backend_job_created", job_id=job_id, video_id=video_id)
        except Exception as exc:
            log.warning("backend_job_create_failed", job_id=job_id, error=str(exc))

    if client is not None:
        _do(client)
    else:
        with httpx.Client() as c:
            _do(c)
