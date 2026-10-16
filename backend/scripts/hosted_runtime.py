"""Supervise API and CPU processing inside the existing Cloudflare Container.

Only standard-library imports live here. Model packages remain in /opt/worker;
the API runs in /opt/api. Worker credentials are private deployment secrets.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

_stop = threading.Event()


def event(name: str, **fields: object) -> None:
    print(json.dumps({"event": name, **fields}), flush=True)


def processing_enabled(environment: Mapping[str, str]) -> bool:
    configured = bool(environment.get("WORKER_EMAIL") and environment.get("WORKER_PASSWORD"))
    requested = environment.get("HOSTED_PROCESSING_ENABLED", "auto").strip().lower()
    if requested in {"0", "false", "off"}:
        return False
    if requested in {"1", "true", "on"} and not configured:
        event("hosted_processing_disabled", reason="Worker service-account secrets are missing")
    return configured


def terminate(process: subprocess.Popen[bytes] | None, grace: float) -> None:
    if process is None or process.poll() is not None:
        return
    # Signal Python first so cooperative stage checks can save/pause the job.
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        # FFmpeg children are in this child's session; clean them up too.
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def wait_for_api(process: subprocess.Popen[bytes], timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while not _stop.is_set() and process.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        _stop.wait(0.5)
    return False


def seed_worker(environment: dict[str, str]) -> bool:
    """Idempotently create only the configured service account; never reset it."""
    seed_environment = {
        **environment,
        "SEED_ADMIN_EMAIL": "",
        "SEED_ADMIN_PASSWORD": "",
        "SEED_WORKER_EMAIL": environment["WORKER_EMAIL"],
        "SEED_WORKER_PASSWORD": environment["WORKER_PASSWORD"],
        "SEED_RESET_PASSWORDS": "0",
    }
    try:
        result = subprocess.run(  # noqa: S603
            ["/opt/api/bin/python", "-m", "scripts.bootstrap_hosted_worker"],
            cwd="/app",
            env=seed_environment,
            timeout=60,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def migrate(environment: dict[str, str]) -> bool:
    """Do not serve new application code against an old schema."""
    try:
        result = subprocess.run(  # noqa: S603
            ["/opt/api/bin/alembic", "upgrade", "head"],
            cwd="/app",
            env=environment,
            capture_output=True,
            timeout=120,
            check=False,
        )
        ok = result.returncode == 0
        fields: dict[str, object] = {"ready": ok, "exit_code": result.returncode}
        if not ok:
            # Name the failure without its message: alembic output can echo
            # the connection string, so only exception class names are logged
            # (enough to tell a bad URL from a broken migration).
            output = result.stderr or result.stdout
            text = output.decode(errors="replace") if isinstance(output, bytes) else ""
            names = re.findall(r"([A-Z][A-Za-z]*(?:Error|Exception))", text)
            fields["error_types"] = sorted(set(names))[:5]
        event("hosted_schema_migration", **fields)
        return ok
    except (OSError, subprocess.TimeoutExpired):
        event("hosted_schema_migration", ready=False, reason="Migration failed or timed out")
        return False


def recover_hosted_jobs(environment: dict[str, str]) -> None:
    token = environment.get("SCHEDULER_TOKEN")
    if not token:
        return
    request = urllib.request.Request(
        "http://127.0.0.1:8000/internal/runtime/recover",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            event("hosted_job_recovery", ready=response.status == 200)
    except (OSError, urllib.error.URLError):
        event("hosted_job_recovery", ready=False)


def start_worker(environment: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        ["/opt/worker/bin/python", "/worker/__main__.py"],
        cwd="/worker",
        env=environment,
        start_new_session=True,
    )


def main() -> int:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: _stop.set())
    environment = dict(os.environ)
    environment["BACKEND_API_URL"] = "http://127.0.0.1:8000"
    environment["WORKER_ID"] = "cloudflare-cpu-primary"
    environment["PIPELINE_DEVICE"] = "cpu"
    environment["PIPELINE_HOSTED"] = "1"
    if not migrate(environment):
        return 1
    enabled = processing_enabled(environment)
    api = subprocess.Popen(  # noqa: S603
        [
            "/opt/api/bin/uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",  # noqa: S104
            "--port",
            "8000",
            "--workers",
            "1",
        ],
        cwd="/app",
        # Only the dedicated helper bootstraps a worker account. The generic
        # API seed can still create a configured admin without changing staff.
        env={**environment, "SEED_WORKER_EMAIL": "", "SEED_WORKER_PASSWORD": ""},
        start_new_session=True,
    )
    worker: subprocess.Popen[bytes] | None = None
    try:
        if not wait_for_api(api):
            event("hosted_api_start_failed")
            return 1
        if enabled and not seed_worker(environment):
            event("hosted_processing_disabled", reason="Service-account bootstrap failed")
            enabled = False
        recover_hosted_jobs(environment)
        if enabled:
            # Check the installed model in the inference environment. Failure
            # leaves film review available without advertising usable tracking.
            try:
                verification = subprocess.run(  # noqa: S603
                    [
                        "/opt/worker/bin/python",
                        "/worker/scripts/install_pretrained.py",
                        "--output",
                        environment["MODEL_DETECT_PATH"],
                        "--verify-only",
                    ],
                    env=environment,
                    timeout=90,
                    check=False,
                )
                detector_ready = verification.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                detector_ready = False
            event("hosted_detector_check", ready=detector_ready)
            if not detector_ready:
                environment["MODEL_DETECT_PATH"] = ""
                environment["WORKER_PROFILES"] = "review"
                environment["PROCESSING_PROFILE"] = "review"
        else:
            event(
                "hosted_processing_disabled",
                reason="Configure worker service-account secrets to enable",
            )
        if enabled:
            worker = start_worker(environment)
        while not _stop.wait(1):
            if api.poll() is not None:
                event("hosted_api_exited", exit_code=api.returncode)
                return api.returncode or 1
            if worker is not None and worker.poll() is not None:
                event("hosted_worker_restarting", exit_code=worker.returncode)
                if not _stop.wait(5):
                    worker = start_worker(environment)
        return 0
    finally:
        # The API stays up for the worker's fenced pause/status writebacks.
        terminate(worker, grace=35)
        terminate(api, grace=10)


if __name__ == "__main__":
    raise SystemExit(main())
