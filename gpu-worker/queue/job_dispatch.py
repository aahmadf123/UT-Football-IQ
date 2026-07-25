"""Job dispatch — creates a processing job the worker can claim.

The backend's ``processing_jobs`` table *is* the queue: inserting a row makes
the job claimable by any running GPU worker (``POST /api/v1/jobs/claim``,
``FOR UPDATE SKIP LOCKED``). There is no separate message broker.

Priority decision rule:
  - ``is_same_session=True`` → SAME_SESSION_PRIORITY (10), period-break work
    that must land inside the coaching feedback window.
  - otherwise → NIGHTLY_PRIORITY (0), full-session batch work.

Environment variables:
  BACKEND_API_URL — backend base URL. Required; dispatch has nowhere to go
                    without it.
"""

from __future__ import annotations

import os
import uuid
from queue.same_session_queue import NIGHTLY_PRIORITY, SAME_SESSION_PRIORITY
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


def _backend_api_url() -> str:
    """Read the backend URL at call time so tests and reloads see changes."""
    return os.environ.get("BACKEND_API_URL", "")


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
    """Create a claimable processing job for a freshly uploaded video.

    Args:
        video_id:        UUID of the video record.
        storage_uri:     Storage URI of the uploaded video (``local://…`` or
                         ``s3://…``); ``pipeline.storage`` parses the scheme.
        job_type:        Pipeline entry point (e.g. ``"ingest"``).
        is_same_session: If True the job is queued at same-session priority.
        clip_id:         Optional clip UUID for clip-scoped jobs.
        input_artifacts: Optional dict of artifacts forwarded to the job.
        client:          Optional shared httpx.Client (created internally if absent).

    Returns:
        Dict with keys ``job_id``, ``priority``, and ``created`` (False when
        the insert failed — the caller decides whether to retry).

    Raises:
        RuntimeError: If ``BACKEND_API_URL`` is unset — there is nowhere to
            enqueue the job, so silently dropping it would lose the upload.
    """
    backend_url = _backend_api_url()
    if not backend_url:
        raise RuntimeError(
            "BACKEND_API_URL is required to dispatch a job — the backend's "
            "processing_jobs table is the queue."
        )

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
        "job_dispatch",
        job_id=job_id,
        video_id=video_id,
        job_type=job_type,
        is_same_session=is_same_session,
        priority=priority,
    )

    created = _create_backend_job(
        backend_url=backend_url,
        job_id=job_id,
        video_id=video_id,
        job_type=job_type,
        priority=priority,
        pipeline_mode=pipeline_mode,
        input_artifacts=payload,
        client=client,
    )

    return {"job_id": job_id, "priority": priority, "created": created}


# ── Backend job record ────────────────────────────────────────────────────────


def _create_backend_job(
    *,
    backend_url: str,
    job_id: str,
    video_id: str,
    job_type: str,
    priority: int,
    pipeline_mode: str,
    input_artifacts: dict[str, Any],
    client: httpx.Client | None = None,
) -> bool:
    """POST /api/v1/jobs to enqueue the job. Returns True when the row landed."""
    payload: dict[str, Any] = {
        "id": job_id,
        "video_id": video_id,
        "job_type": job_type,
        "priority": priority,
        "pipeline_mode": pipeline_mode,
        "input_artifacts": input_artifacts,
    }
    jobs_url = f"{backend_url.rstrip('/')}/api/v1/jobs"

    headers: dict[str, str] = {}
    try:
        from worker import auth as worker_auth

        bearer = worker_auth.token()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
    except ImportError:
        pass

    def _do(c: httpx.Client) -> bool:
        try:
            resp = c.post(jobs_url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            log.info("backend_job_created", job_id=job_id, video_id=video_id)
            return True
        except Exception as exc:
            log.warning("backend_job_create_failed", job_id=job_id, error=str(exc))
            return False

    if client is not None:
        return _do(client)
    with httpx.Client() as c:
        return _do(c)
