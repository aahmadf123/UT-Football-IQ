"""Pipeline timeout handler — enforces an 8-minute deadline on same-session jobs.

If a period-break job exceeds PERIOD_BREAK_TIMEOUT_SECONDS (480 s) it is
considered unable to meet the coaching-staff delivery window.  The handler:
  1. Raises ``JobTimeoutError`` to interrupt the running job.
  2. Requeues the job to the nightly batch queue with NIGHTLY_PRIORITY (0).
  3. Updates the backend job record to "failed" with a descriptive error message.

Usage::

    from worker.timeout_handler import run_with_timeout, JobTimeoutError

    try:
        result = run_with_timeout(my_stage_fn, args=(clip_id, ...), job_id=job_id)
    except JobTimeoutError:
        # job has already been requeued for nightly — nothing more to do
        pass

Environment variables:
  PERIOD_BREAK_TIMEOUT_SECONDS — wall-clock limit for same-session jobs (default: 480)
  BACKEND_API_URL              — backend base URL for job status callbacks
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable
from queue.same_session_queue import push_nightly_job
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

PERIOD_BREAK_TIMEOUT_SECONDS = int(
    os.environ.get("PERIOD_BREAK_TIMEOUT_SECONDS", "480")
)
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "")


class JobTimeoutError(Exception):
    """Raised when a same-session job exceeds PERIOD_BREAK_TIMEOUT_SECONDS."""


# ── Public API ────────────────────────────────────────────────────────────────


def run_with_timeout(
    fn: Callable[..., Any],
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    *,
    job_id: str,
    job_payload: dict[str, Any] | None = None,
    timeout_seconds: int = PERIOD_BREAK_TIMEOUT_SECONDS,
) -> Any:
    """Run *fn* with a wall-clock timeout; requeue to nightly on expiry.

    Args:
        fn:              The pipeline stage callable to execute.
        args:            Positional arguments forwarded to *fn*.
        kwargs:          Keyword arguments forwarded to *fn*.
        job_id:          Backend job UUID used for status callbacks.
        job_payload:     Original CF Queue payload to requeue if timeout fires.
        timeout_seconds: Deadline in seconds (default: PERIOD_BREAK_TIMEOUT_SECONDS).

    Returns:
        The return value of *fn* on success.

    Raises:
        JobTimeoutError: If *fn* does not complete within *timeout_seconds*.
        Exception:       Any exception raised by *fn* itself is re-raised.
    """
    if kwargs is None:
        kwargs = {}

    result_holder: list[Any] = [None]
    exc_holder: list[BaseException | None] = [None]
    finished = threading.Event()

    def _target() -> None:
        try:
            result_holder[0] = fn(*args, **kwargs)
        except Exception as exc:
            exc_holder[0] = exc
        finally:
            finished.set()

    # daemon=True so the thread does not prevent process exit when the main
    # worker shuts down.  The worst-case outcome of an abrupt kill is a job
    # stuck in "running" state, which the nightly cleanup sweep already handles
    # by requeuing any job started more than PERIOD_BREAK_TIMEOUT_SECONDS ago.
    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    completed = finished.wait(timeout=float(timeout_seconds))

    if not completed:
        log.warning(
            "job_timeout",
            job_id=job_id,
            timeout_seconds=timeout_seconds,
        )
        _handle_timeout(
            job_id=job_id,
            job_payload=job_payload,
            timeout_seconds=timeout_seconds,
        )
        raise JobTimeoutError(
            f"Job {job_id} exceeded {timeout_seconds}s timeout — requeued for nightly processing"
        )

    if exc_holder[0] is not None:
        raise exc_holder[0]

    return result_holder[0]


# ── Timeout handling ──────────────────────────────────────────────────────────


def _handle_timeout(
    job_id: str,
    job_payload: dict[str, Any] | None,
    timeout_seconds: int,
) -> None:
    """Mark the job failed in the backend and requeue it for nightly processing."""
    error_msg = (
        f"Same-session job exceeded {timeout_seconds}s window; "
        "requeued for nightly full-quality processing."
    )
    try:
        _update_job_failed(job_id, error_msg)
    except Exception as exc:
        log.warning("timeout_handle_backend_error", job_id=job_id, error=str(exc))

    if job_payload is not None:
        _requeue_for_nightly(job_id, job_payload)


def _update_job_failed(job_id: str, error_message: str) -> None:
    if not BACKEND_API_URL:
        return
    from worker.auth import worker_id

    payload: dict[str, Any] = {
        "status": "failed",
        "error_stage": "timeout",
        "error_message": error_message,
        "worker_id": worker_id(),
    }
    try:
        from worker import auth as worker_auth

        headers: dict[str, str] = {}
        bearer = worker_auth.token()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        with httpx.Client(base_url=BACKEND_API_URL, timeout=10, headers=headers) as c:
            c.patch(f"/api/v1/jobs/{job_id}", json=payload)
        log.info("timeout_job_marked_failed", job_id=job_id)
    except Exception as exc:
        log.warning("timeout_status_update_failed", job_id=job_id, error=str(exc))


def _requeue_for_nightly(job_id: str, job_payload: dict[str, Any]) -> None:
    """Push the job onto the nightly queue with NIGHTLY_PRIORITY."""
    from queue.same_session_queue import NIGHTLY_PRIORITY

    nightly_payload = {**job_payload, "priority": NIGHTLY_PRIORITY, "_requeuedFrom": job_id}
    try:
        msg_id = push_nightly_job(nightly_payload)
        log.info("job_requeued_for_nightly", original_job_id=job_id, message_id=msg_id)
    except Exception as exc:
        log.error("nightly_requeue_failed", job_id=job_id, error=str(exc))


# ── SIGALRM-based timeout (Unix only, for single-threaded contexts) ───────────


def _sigalrm_handler(signum: int, _frame: Any) -> None:  # pragma: no cover
    raise JobTimeoutError(f"SIGALRM fired after {PERIOD_BREAK_TIMEOUT_SECONDS}s")


def set_unix_alarm(seconds: int = PERIOD_BREAK_TIMEOUT_SECONDS) -> None:  # pragma: no cover
    """Arm a SIGALRM as a secondary safety net (Unix only)."""
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _sigalrm_handler)
        signal.alarm(seconds)


def clear_unix_alarm() -> None:  # pragma: no cover
    """Disarm the SIGALRM after successful job completion."""
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)
