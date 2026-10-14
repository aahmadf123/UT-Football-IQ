"""Local CPU processing for an existing backend; never connects to its database."""

from __future__ import annotations

import getpass
import os
import signal
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from local_app.config import LocalConfig, atomic_json, read_dotenv
from local_app.instance import InstanceLock
from local_app.supervisor import Service, Supervisor


def run_connected(
    config: LocalConfig,
    api_url: str,
    frontend_url: str | None,
    no_browser: bool,
    skip_model_install: bool,
) -> int:
    lock = InstanceLock(config.data)
    if not lock.acquire():
        print(
            "This library already has a running launcher. Quit it before changing its connection.",
            file=sys.stderr,
        )
        return 1
    try:
        return _run_connected(
            config, api_url, frontend_url, no_browser, skip_model_install
        )
    except (OSError, ValueError) as error:
        print(f"Connected processing could not start: {error}", file=sys.stderr)
        return 1
    finally:
        lock.release()


def _run_connected(
    config: LocalConfig,
    api_url: str,
    frontend_url: str | None,
    no_browser: bool,
    skip_model_install: bool,
) -> int:
    from local_app.__main__ import command

    remote = urlparse(api_url)
    if (
        remote.username
        or remote.password
        or not remote.hostname
        or remote.query
        or remote.fragment
        or remote.scheme not in {"http", "https"}
        or (
            remote.scheme != "https"
            and remote.hostname not in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        print(
            "Use an HTTPS team API URL, or a loopback development API.", file=sys.stderr
        )
        return 1
    if frontend_url:
        website = urlparse(frontend_url)
        if (
            website.scheme not in {"http", "https"}
            or not website.hostname
            or website.username
            or website.password
            or (
                website.scheme != "https"
                and website.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            print(
                "Use an HTTPS film-room address, or a loopback development site.",
                file=sys.stderr,
            )
            return 1
    values = read_dotenv(config.root / ".env")
    email = os.environ.get("WORKER_EMAIL") or values.get("WORKER_EMAIL")
    password = os.environ.get("WORKER_PASSWORD") or values.get("WORKER_PASSWORD")
    if not email or not password:
        if not sys.stdin.isatty():
            print(
                "Connected processing needs WORKER_EMAIL and WORKER_PASSWORD for a staff account.",
                file=sys.stderr,
            )
            return 1
        email = email or input("Team processing account email: ").strip()
        password = password or getpass.getpass("Team processing account password: ")
    environment = config.environment()
    environment.update(
        {
            "FOOTBALL_IQ_BUNDLE_ROOT": str(config.root),
            "PYTHONPATH": str(config.root)
            + os.pathsep
            + environment.get("PYTHONPATH", ""),
            "BACKEND_API_URL": api_url.rstrip("/"),
            "PIPELINE_STORAGE_TRANSPORT": "api",
            "WORKER_EMAIL": email,
            "WORKER_PASSWORD": password,
        }
    )
    supervisor = Supervisor(
        config.data, api_url.rstrip("/"), mode="connected", frontend_url=frontend_url
    )
    stopped = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)
    try:
        worker = Service("worker", command(config, "worker"), config.root, environment)
        supervisor.add(worker)
        if not supervisor.start(worker):
            return 1
        if (
            not skip_model_install
            and not Path(environment["MODEL_DETECT_PATH"]).is_file()
        ):
            atomic_json(
                config.data / "runtime/controls/initial-model.json",
                {"action": "install-detector"},
            )
        print(f"Local CPU processing connected to {api_url.rstrip('/')}")
        if frontend_url and not no_browser:
            webbrowser.open(frontend_url)
        while not stopped:
            supervisor.controls(
                command(config, "install-detector"), config.root, environment
            )
            if supervisor.stop_requested:
                break
            supervisor.tick()
            time.sleep(2)
        return 0
    finally:
        supervisor.shutdown()
