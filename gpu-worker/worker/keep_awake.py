"""Keep a Windows laptop from sleeping while a job is being processed.

The staff laptop is the team's only GPU worker. Windows' idle-sleep timer
would otherwise suspend it halfway through a long game file, leaving the job's
lease to expire. ``SetThreadExecutionState`` asks Windows to stay awake for as
long as the flag is held and lets normal power management resume afterwards;
the display may still turn off. A no-op on other platforms.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

log = structlog.get_logger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextmanager
def keep_awake() -> Iterator[None]:
    if sys.platform != "win32":
        yield
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    held = bool(kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
    if not held:
        log.warning("keep_awake_unavailable")
    try:
        yield
    finally:
        if held:
            kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
