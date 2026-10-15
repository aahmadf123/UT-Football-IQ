"""Bounded self-healing with durable diagnostics and graceful process cleanup."""

from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from local_app.config import atomic_json


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def healthy(url: str, timeout: float = 2) -> bool:
    try:
        # Loopback health checks must not be routed to an outbound HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


@dataclass
class Service:
    name: str
    command: list[str]
    cwd: Path
    environment: dict[str, str]
    health_url: str | None = None
    stop_command: list[str] | None = None
    restart: bool = True
    process: subprocess.Popen[bytes] | None = None
    log: BinaryIO | None = None
    state: str = "stopped"
    last_error: str | None = None
    starts: collections.deque[float] = field(default_factory=collections.deque)
    next_start: float = 0
    started_at: float = 0
    health_failures: int = 0
    restarts: int = 0

    def __post_init__(self) -> None:
        # API, worker and installer start from the same launcher environment.
        # Stop-file paths belong to one child and must never overwrite another.
        self.environment = dict(self.environment)


class Supervisor:
    def __init__(
        self,
        data: Path,
        api_url: str,
        *,
        restart_limit: int = 5,
        mode: str = "local",
        frontend_url: str | None = None,
    ) -> None:
        self.data = data
        self.api_url = api_url
        self.restart_limit = restart_limit
        self.mode = mode
        self.frontend_url = frontend_url
        self.services: dict[str, Service] = {}
        self.events: collections.deque[dict[str, str]] = collections.deque(maxlen=30)
        self.started_at = timestamp()
        self.stopping = False
        self.stop_requested = False

    def event(self, service: str, message: str) -> None:
        self.events.append({"at": timestamp(), "service": service, "message": message})

    def add(self, service: Service) -> None:
        if service.name in self.services:
            raise ValueError(f"Duplicate service: {service.name}")
        self.services[service.name] = service

    def publish(self) -> None:
        states = {service.state for service in self.services.values()}
        state = (
            "stopped"
            if self.stopping
            else ("needs_attention" if "failed" in states else "running")
        )
        atomic_json(
            self.data / "runtime" / "status.json",
            {
                "mode": self.mode,
                "state": state,
                "started_at": self.started_at,
                "updated_at": timestamp(),
                "api_url": self.api_url,
                "frontend_url": self.frontend_url,
                "services": [
                    {
                        "name": service.name,
                        "state": service.state,
                        "pid": service.process.pid
                        if service.process and service.process.poll() is None
                        else None,
                        "restarts": service.restarts,
                        "last_error": service.last_error,
                    }
                    for service in self.services.values()
                ],
                "events": list(self.events),
            },
        )

    def start(self, service: Service) -> bool:
        now = time.monotonic()
        while service.starts and now - service.starts[0] > 600:
            service.starts.popleft()
        if len(service.starts) >= self.restart_limit:
            service.state = "failed"
            service.last_error = (
                "Restart limit reached. Check diagnostics before retrying."
            )
            self.event(service.name, service.last_error)
            self.publish()
            return False
        service.starts.append(now)
        if service.log:
            service.log.close()
        service.log = (self.data / "logs" / f"{service.name}.log").open("ab")
        options: dict[str, object] = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            if service.name in {"api", "worker"}:
                stop_file = self.data / "runtime" / f"{service.name}.stop"
                stop_file.unlink(missing_ok=True)
                service.environment["LOCAL_APP_STOP_FILE"] = str(stop_file)
            service.process = subprocess.Popen(
                service.command,
                cwd=service.cwd,
                env=service.environment,
                stdin=subprocess.DEVNULL,
                stdout=service.log,
                stderr=subprocess.STDOUT,
                **options,
            )
        except OSError:
            service.state = "failed"
            service.last_error = (
                "Service could not start. Check installed runtime and diagnostics."
            )
            service.log.close()
            service.log = None
            self.publish()
            return False
        service.state = "starting" if service.health_url else "running"
        service.started_at = now
        service.health_failures = 0
        service.last_error = None
        self.event(service.name, "Started")
        self.publish()
        return True

    def stop(self, service: Service) -> None:
        process = service.process
        if process and process.poll() is None:
            try:
                stop_file = service.environment.get("LOCAL_APP_STOP_FILE")
                if service.stop_command:
                    subprocess.run(
                        service.stop_command,
                        cwd=service.cwd,
                        env=service.environment,
                        stdout=service.log,
                        stderr=subprocess.STDOUT,
                        timeout=12,
                        check=False,
                    )
                elif stop_file:
                    # A hidden Windows GUI has no console for CTRL_BREAK.
                    # Services watch this private control file on every OS.
                    Path(stop_file).touch()
                elif sys.platform == "win32":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                    )
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=5)
        if service.log:
            service.log.close()
            service.log = None
        service.process = None
        service.state = "stopped"

    def tick(self) -> None:
        now = time.monotonic()
        for service in list(self.services.values()):
            process = service.process
            if process is None:
                if service.state == "recovering" and now >= service.next_start:
                    self.start(service)
                continue
            code = process.poll()
            if code is not None:
                service.process = None
                if service.log:
                    service.log.close()
                    service.log = None
                if not service.restart:
                    service.state = "complete" if code == 0 else "failed"
                    service.last_error = (
                        None
                        if code == 0
                        else f"Exited with code {code}; see diagnostics."
                    )
                    if (
                        code == 0
                        and service.name == "install-detector"
                        and "worker" in self.services
                    ):
                        self.restart_service("worker")
                    continue
                service.restarts += 1
                service.state = "recovering"
                service.last_error = (
                    f"Exited with code {code}; automatic restart scheduled."
                )
                service.next_start = now + min(30, 2 ** min(service.restarts, 5))
                self.event(service.name, service.last_error)
                continue
            if service.health_url:
                if healthy(service.health_url):
                    service.state = "running"
                    service.health_failures = 0
                elif now - service.started_at > 60:
                    service.health_failures += 1
                    if service.health_failures >= 5:
                        self.stop(service)
                        service.restarts += 1
                        service.state = "recovering"
                        service.last_error = "Health check stopped responding; automatic restart scheduled."
                        service.next_start = now + 2
                        self.event(service.name, service.last_error)
        self.publish()

    def restart_service(self, name: str, *, requested: bool = False) -> None:
        service = self.services.get(name)
        if service is None:
            self.event(name, "Service is not configured")
            return
        self.stop(service)
        if requested:
            service.starts.clear()
            service.restarts = 0
        self.start(service)

    def controls(
        self, install_command: list[str], install_cwd: Path, environment: dict[str, str]
    ) -> None:
        for path in sorted((self.data / "runtime" / "controls").glob("*.json")):
            try:
                command = json.loads(path.read_text(encoding="utf-8"))
                action = command.get("action")
                if action == "shutdown":
                    self.stop_requested = True
                elif action in {"restart-worker", "restart-api"}:
                    self.restart_service(
                        action.removeprefix("restart-"), requested=True
                    )
                elif action == "install-detector":
                    task = self.services.get("install-detector")
                    if task is None or task.process is None:
                        if task:
                            self.services.pop(task.name)
                        task = Service(
                            "install-detector",
                            install_command,
                            install_cwd,
                            environment,
                            restart=False,
                        )
                        self.add(task)
                        self.start(task)
                elif action == "clear-cache":
                    cleared = 0
                    # Only disposable cache files. Never recurse into storage,
                    # models, or checkpoints.
                    for cached in (self.data / "cache").rglob("*"):
                        if cached.is_file() and not cached.is_symlink():
                            cached.unlink()
                            cleared += 1
                    self.event("cache", f"Removed {cleared} disposable cache files")
                else:
                    self.event("launcher", "Unsupported repair request")
            except (OSError, ValueError):
                self.event("launcher", "Repair request could not be read")
            finally:
                path.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self.stopping = True
        for service in reversed(list(self.services.values())):
            self.stop(service)
        self.publish()
