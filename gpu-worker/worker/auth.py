"""Backend authentication for the GPU worker (service-account login).

Every backend writeback route requires a staff Bearer JWT, so the worker
logs in as its seeded service account (``WORKER_EMAIL`` / ``WORKER_PASSWORD``,
role ``analyst``) and caches the access token, re-logging in before the
token's TTL elapses or when a caller reports a 401.

When no credentials are configured, ``token()`` returns ``None`` and callers
proceed unauthenticated — offline/CLI runs and unauthenticated dev backends
keep working.
"""

from __future__ import annotations

import os
import threading
import time

import httpx
import structlog

log = structlog.get_logger(__name__)

# Re-login a comfortable margin before the backend's 60-minute access TTL.
_DEFAULT_TTL_SECONDS = 45 * 60
# After a failed login, don't retry for this long: a stalled backend would
# otherwise eat a full login round-trip on every writeback call.
_LOGIN_BACKOFF_SECONDS = 30.0


def worker_id() -> str:
    """Stable identity for job leases: ``WORKER_ID`` env or hostname-pid."""
    import socket

    return os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"


class WorkerAuth:
    """Thread-safe cached login against the backend auth API."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._acquired_at: float = 0.0
        self._last_failure_at: float = 0.0

    @property
    def _configured(self) -> bool:
        return bool(
            os.environ.get("BACKEND_API_URL")
            and os.environ.get("WORKER_EMAIL")
            and os.environ.get("WORKER_PASSWORD")
        )

    def token(self) -> str | None:
        """Return a fresh access token, logging in when needed."""
        if not self._configured:
            return None
        ttl = int(os.environ.get("WORKER_TOKEN_TTL_SECONDS", str(_DEFAULT_TTL_SECONDS)))
        with self._lock:
            if self._token and (time.monotonic() - self._acquired_at) < ttl:
                return self._token
            if (time.monotonic() - self._last_failure_at) < _LOGIN_BACKOFF_SECONDS:
                return None  # recent login failure — don't hammer the backend
            return self._login_locked()

    def invalidate(self) -> None:
        """Drop the cached token (call after a 401) — next use re-logins."""
        with self._lock:
            self._token = None
            self._last_failure_at = 0.0  # a 401 means the backend is up: retry now

    def _login_locked(self) -> str | None:
        base = os.environ.get("BACKEND_API_URL", "")
        try:
            resp = httpx.post(
                f"{base.rstrip('/')}/api/v1/auth/login",
                json={
                    "email": os.environ.get("WORKER_EMAIL", ""),
                    "password": os.environ.get("WORKER_PASSWORD", ""),
                },
                timeout=15,
            )
            resp.raise_for_status()
            self._token = str(resp.json()["access_token"])
            self._acquired_at = time.monotonic()
            self._last_failure_at = 0.0
            log.info("worker_auth_login_ok")
            return self._token
        except Exception as exc:
            log.error("worker_auth_login_failed", error=str(exc))
            self._token = None
            self._last_failure_at = time.monotonic()
            return None


_AUTH = WorkerAuth()


def token() -> str | None:
    """Module-level accessor used by pipeline.backend and the worker loop."""
    return _AUTH.token()


def invalidate() -> None:
    _AUTH.invalidate()
