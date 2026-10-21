"""Keep a laptop worker inside its thermal and power envelope.

The RTX 5060 laptop powered off mid-job five times in four days: no
bugcheck, no power button, no sleep -- the firmware cutting power under
sustained full load. The worker now paces itself:

* **Cool-down.** Every few seconds (from :func:`pipeline.runtime.check_control`,
  which every processing loop already calls) the GPU temperature is read.
  At ``WORKER_GPU_HOT_C`` (80 °C) work pauses until the GPU is back to
  ``WORKER_GPU_COOL_C`` (70 °C). Cancels and pauses still apply while it
  waits, and the lease heartbeat runs on its own thread.
* **Fewer CPU threads.** OpenCV, PyTorch and ffmpeg each default to every
  core; :func:`cpu_threads` caps them at half the logical cores
  (``WORKER_CPU_THREADS`` overrides) so the CPU never sits at 100 %.
* **Lower priority.** :func:`lower_process_priority` runs the worker (and
  the ffmpeg processes it starts, which inherit the class) below normal, so
  the laptop stays responsive.

CPU temperature is not readable without administrator rights on Windows;
the thread cap and the power plan's processor limit cover the CPU side.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable

import structlog

log = structlog.get_logger(__name__)

GPU_HOT_C = float(os.environ.get("WORKER_GPU_HOT_C", "80"))
GPU_COOL_C = float(os.environ.get("WORKER_GPU_COOL_C", "70"))
#: Seconds between temperature reads; a read costs ~50 ms of nvidia-smi.
CHECK_INTERVAL_S = 5.0
#: Never wait longer than this for one cool-down; work resumes regardless.
MAX_COOL_DOWN_S = 15 * 60.0


def cpu_threads() -> int:
    """Threads any one library may use: half the logical cores, at least 2."""
    configured = os.environ.get("WORKER_CPU_THREADS")
    if configured:
        return max(1, int(configured))
    return max(2, (os.cpu_count() or 4) // 2)


def gpu_temperature() -> float | None:
    """Hottest NVIDIA GPU in °C, or ``None`` when it can't be read."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        readings = [float(line) for line in result.stdout.split() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return max(readings) if readings else None


class ThermalGovernor:
    """Pauses work while the GPU is hot. Cheap to call often."""

    def __init__(
        self,
        *,
        read: Callable[[], float | None] = gpu_temperature,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        hot_c: float = GPU_HOT_C,
        cool_c: float = GPU_COOL_C,
    ) -> None:
        self._read = read
        self._sleep = sleep
        self._clock = clock
        self.hot_c = hot_c
        self.cool_c = min(cool_c, hot_c - 1)
        self._next_check = 0.0
        self.cool_downs = 0

    def pace(self, still_wanted: Callable[[], None] = lambda: None) -> None:
        """Return at once unless the GPU is hot; then wait for it to cool.

        ``still_wanted`` runs while waiting so a cancel or pause raises out.
        """
        now = self._clock()
        if now < self._next_check:
            return
        self._next_check = now + CHECK_INTERVAL_S
        temperature = self._read()
        if temperature is None or temperature < self.hot_c:
            return
        self.cool_downs += 1
        started = now
        log.warning("thermal_cool_down_start", gpu_c=temperature, resume_at_c=self.cool_c)
        while temperature is not None and temperature > self.cool_c:
            if self._clock() - started > MAX_COOL_DOWN_S:
                log.warning("thermal_cool_down_timeout", gpu_c=temperature)
                break
            still_wanted()
            self._sleep(CHECK_INTERVAL_S)
            temperature = self._read()
        log.info("thermal_cool_down_done", gpu_c=temperature, seconds=round(self._clock() - started, 1))
        self._next_check = self._clock() + CHECK_INTERVAL_S


governor = ThermalGovernor()


def lower_process_priority() -> None:
    """Run below normal priority; child processes inherit it."""
    try:
        if sys.platform == "win32":
            import ctypes

            below_normal = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
            ctypes.windll.kernel32.SetPriorityClass(handle, below_normal)  # type: ignore[attr-defined]
        else:
            os.nice(5)
    except Exception as exc:  # noqa: BLE001 - pacing only
        log.warning("priority_unchanged", error=str(exc))


def limit_library_threads() -> int:
    """Cap OpenCV and PyTorch CPU threads; returns the cap."""
    threads = cpu_threads()
    try:
        import cv2

        cv2.setNumThreads(threads)
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch

        torch.set_num_threads(threads)
    except Exception:  # noqa: BLE001
        pass
    return threads
