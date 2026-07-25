"""Same-session high-priority queue — separate from the nightly batch queue.

Same-session jobs (period-break clips) are dispatched to a dedicated
Cloudflare Queue and processed with elevated priority so coaching staff
receive feedback within the 5–10 min period-break window.

Priority levels (stored on processing_jobs.priority):
  SAME_SESSION_PRIORITY = 10  — period-break / real-time jobs
  NIGHTLY_PRIORITY      = 0   — full-session nightly processing

Environment variables:
  CLOUDFLARE_ACCOUNT_ID    — Cloudflare account ID
  CLOUDFLARE_API_TOKEN     — Cloudflare API token with Queues write permission
  CF_QUEUE_SAME_SESSION    — queue name (default: same-session-jobs)
  CF_QUEUE_VIDEO_PROCESSING — nightly queue name (default: video-processing-jobs)
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")

# Dedicated same-session queue — kept separate from nightly batch so a surge of
# full-session jobs cannot delay period-break clip delivery.
SAME_SESSION_QUEUE = os.environ.get("CF_QUEUE_SAME_SESSION", "same-session-jobs")
NIGHTLY_QUEUE = os.environ.get("CF_QUEUE_VIDEO_PROCESSING", "video-processing-jobs")

# Priority values used in the processing_jobs.priority column.
SAME_SESSION_PRIORITY: int = 10
NIGHTLY_PRIORITY: int = 0

_CF_BASE = "https://api.cloudflare.com/client/v4/accounts"


def _push_url(queue_name: str) -> str:
    return f"{_CF_BASE}/{ACCOUNT_ID}/queues/{queue_name}/messages"


def _pull_url(queue_name: str) -> str:
    return f"{_CF_BASE}/{ACCOUNT_ID}/queues/{queue_name}/messages/pull"


def _ack_url(queue_name: str) -> str:
    return f"{_CF_BASE}/{ACCOUNT_ID}/queues/{queue_name}/messages/ack"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def _cf_queues_enabled() -> bool:
    """CF queue publishing is opt-in; DB-as-queue is the default transport."""
    return os.environ.get("CF_QUEUES_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


# ── Push helpers ──────────────────────────────────────────────────────────────


def push_same_session_job(
    job_payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> str:
    """Enqueue a period-break job on the same-session high-priority queue.

    Returns the message ID assigned by Cloudflare.
    """
    return _push(SAME_SESSION_QUEUE, job_payload, client=client)


def push_nightly_job(
    job_payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> str:
    """Enqueue a full-session job on the nightly batch queue."""
    return _push(NIGHTLY_QUEUE, job_payload, client=client)


def _push(
    queue_name: str,
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> str:
    if not _cf_queues_enabled():
        # DB-as-queue mode (default): the backend processing_jobs row created
        # alongside this push IS the queue entry, so the CF publish is a no-op.
        log.info("cf_queue_push_skipped_db_mode", queue=queue_name, job_id=payload.get("jobId"))
        return "db-only"

    body = json.dumps({"messages": [{"body": payload}]})
    log.info("cf_queue_push", queue=queue_name, job_id=payload.get("jobId"))

    def _do(c: httpx.Client) -> str:
        resp = c.post(
            _push_url(queue_name),
            content=body,
            headers=_auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        message_id: str = (
            data.get("result", {})
            .get("messages", [{}])[0]
            .get("id", "")
        )
        log.info("cf_queue_push_ok", queue=queue_name, message_id=message_id)
        return message_id

    if client is not None:
        return _do(client)
    with httpx.Client() as c:
        return _do(c)


# ── Pull / ack helpers ────────────────────────────────────────────────────────


def pull_same_session_messages(
    batch_size: int = 5,
    visibility_timeout_ms: int = 60_000,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Pull up to *batch_size* messages from the same-session queue."""
    return _pull(
        SAME_SESSION_QUEUE,
        batch_size=batch_size,
        visibility_timeout_ms=visibility_timeout_ms,
        client=client,
    )


def ack_same_session_message(
    lease_id: str,
    *,
    client: httpx.Client | None = None,
) -> None:
    """Acknowledge (delete) a processed same-session message."""
    _ack(SAME_SESSION_QUEUE, lease_id, client=client)


def _pull(
    queue_name: str,
    *,
    batch_size: int = 5,
    visibility_timeout_ms: int = 60_000,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    log.debug("cf_queue_pull", queue=queue_name, batch_size=batch_size)

    def _do(c: httpx.Client) -> list[dict[str, Any]]:
        resp = c.post(
            _pull_url(queue_name),
            headers=_auth_headers(),
            json={"batch_size": batch_size, "visibility_timeout_ms": visibility_timeout_ms},
            timeout=30,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        messages: list[dict[str, Any]] = data.get("result", {}).get("messages", [])
        log.debug("cf_queue_pull_ok", queue=queue_name, count=len(messages))
        return messages

    if client is not None:
        return _do(client)
    with httpx.Client() as c:
        return _do(c)


def _ack(
    queue_name: str,
    lease_id: str,
    *,
    client: httpx.Client | None = None,
) -> None:
    def _do(c: httpx.Client) -> None:
        resp = c.post(
            _ack_url(queue_name),
            headers=_auth_headers(),
            json={"acks": [{"lease_id": lease_id}]},
            timeout=15,
        )
        resp.raise_for_status()
        log.debug("cf_queue_ack_ok", queue=queue_name, lease_id=lease_id)

    if client is not None:
        _do(client)
    else:
        with httpx.Client() as c:
            _do(c)


# ── Queue depth probe ─────────────────────────────────────────────────────────


def get_same_session_queue_depth(
    *,
    client: httpx.Client | None = None,
) -> int:
    """Return the approximate number of messages waiting in the same-session queue.

    Returns 0 on any error so callers degrade gracefully.
    """
    return _get_queue_depth(SAME_SESSION_QUEUE, client=client)


def _get_queue_depth(
    queue_name: str,
    *,
    client: httpx.Client | None = None,
) -> int:
    url = f"{_CF_BASE}/{ACCOUNT_ID}/queues/{queue_name}"

    def _do(c: httpx.Client) -> int:
        try:
            resp = c.get(url, headers=_auth_headers(), timeout=10)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            depth: int = int(
                data.get("result", {}).get("stats", {}).get("totalMessages", 0)
            )
            return depth
        except Exception as exc:
            log.warning("cf_queue_depth_error", queue=queue_name, error=str(exc))
            return 0

    if client is not None:
        return _do(client)
    with httpx.Client() as c:
        return _do(c)
