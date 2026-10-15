"""Interruptible local media commands with bounded cleanup."""

from __future__ import annotations

import subprocess
import tempfile
import time

from pipeline.runtime import check_control


def run_checked(command: list[str], *, timeout: float = 180) -> None:
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_control()
                if time.monotonic() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    continue
            check_control()
            if process.returncode:
                errors.seek(0)
                raise subprocess.CalledProcessError(
                    process.returncode, command, stderr=errors.read(16 * 1024)
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
