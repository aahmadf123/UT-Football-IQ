"""GPU worker auto-scaling — burst a GPU instance when the same-session queue is non-empty.

Checks the same-session queue depth on each poll cycle and calls out to the
configured scaling API (Cloudflare Workers / external orchestrator) to request
a burst instance.  Scales back to baseline when the queue drains.

Environment variables:
  AUTOSCALE_API_URL        — endpoint to POST scale-up / scale-down requests.
                             Leave empty to disable auto-scaling (dry-run mode).
  AUTOSCALE_API_TOKEN      — Bearer token for the scale API (optional).
  AUTOSCALE_BURST_INSTANCES — number of extra GPU instances to request (default: 1)
  AUTOSCALE_POLL_INTERVAL  — seconds between depth checks (default: 30)
  CLOUDFLARE_ACCOUNT_ID    — forwarded to same_session_queue
  CLOUDFLARE_API_TOKEN     — forwarded to same_session_queue
"""

from __future__ import annotations

import os
import signal
import time
from queue.same_session_queue import get_same_session_queue_depth
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

AUTOSCALE_API_URL = os.environ.get("AUTOSCALE_API_URL", "")
AUTOSCALE_API_TOKEN = os.environ.get("AUTOSCALE_API_TOKEN", "")
BURST_INSTANCES = int(os.environ.get("AUTOSCALE_BURST_INSTANCES", "1"))
POLL_INTERVAL = int(os.environ.get("AUTOSCALE_POLL_INTERVAL", "30"))

_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    log.info("autoscale_shutdown_signal", signal=signum)
    _shutdown = True


def _register_signal_handlers() -> None:
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
    except ValueError:
        log.warning("autoscale_signal_registration_skipped_non_main_thread")


# ── Scale actions ─────────────────────────────────────────────────────────────


def request_burst(
    instances: int = BURST_INSTANCES,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Request additional GPU instances via the autoscale API.

    Returns True when the request succeeded (or when running in dry-run mode
    with no AUTOSCALE_API_URL configured).
    """
    if not AUTOSCALE_API_URL:
        log.info("autoscale_dry_run_burst", instances=instances)
        return True

    payload: dict[str, Any] = {"action": "scale_up", "instances": instances}
    return _call_scale_api(payload, client=client)


def request_scale_down(
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Release burst instances when the same-session queue has drained."""
    if not AUTOSCALE_API_URL:
        log.info("autoscale_dry_run_scale_down")
        return True

    payload: dict[str, Any] = {"action": "scale_down"}
    return _call_scale_api(payload, client=client)


def _call_scale_api(
    payload: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> bool:
    headers: dict[str, str] = {}
    if AUTOSCALE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AUTOSCALE_API_TOKEN}"

    def _do(c: httpx.Client) -> bool:
        try:
            resp = c.post(AUTOSCALE_API_URL, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            log.info("autoscale_api_ok", action=payload.get("action"))
            return True
        except Exception as exc:
            log.error("autoscale_api_error", action=payload.get("action"), error=str(exc))
            return False

    if client is not None:
        return _do(client)
    with httpx.Client() as c:
        return _do(c)


# ── Main polling loop ─────────────────────────────────────────────────────────


def run_autoscale_loop() -> None:
    """Poll the same-session queue depth and auto-scale GPU capacity.

    Intended to run as a lightweight sidecar alongside the main GPU worker.
    """
    log.info("autoscale_loop_starting", poll_interval=POLL_INTERVAL)
    _register_signal_handlers()
    burst_active = False

    with httpx.Client() as client:
        while not _shutdown:
            try:
                depth = get_same_session_queue_depth(client=client)
                log.debug("autoscale_queue_depth", depth=depth)

                if depth > 0 and not burst_active:
                    log.info("autoscale_burst_triggered", queue_depth=depth)
                    if request_burst(client=client):
                        burst_active = True

                elif depth == 0 and burst_active:
                    log.info("autoscale_scale_down_triggered")
                    if request_scale_down(client=client):
                        burst_active = False

            except Exception as exc:
                log.error("autoscale_loop_error", error=str(exc))

            if not _shutdown:
                time.sleep(POLL_INTERVAL)

    log.info("autoscale_loop_stopped")


if __name__ == "__main__":
    run_autoscale_loop()
