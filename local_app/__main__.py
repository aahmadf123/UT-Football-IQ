"""Start Football-IQ with one command, or build/run its native distribution."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from local_app.backup import record_migration, snapshot_before_migration
from local_app.config import LocalConfig, atomic_json
from local_app.database import start_database
from local_app.instance import InstanceLock
from local_app.reopen import local_login_url
from local_app.supervisor import Service, Supervisor, healthy


def command(config: LocalConfig, service: str, *arguments: str) -> list[str]:
    executable_name = "worker" if service in {"worker", "install-detector"} else "api"
    suffix = ".exe" if os.name == "nt" else ""
    packaged = config.root / "services" / executable_name / (executable_name + suffix)
    if packaged.is_file():
        return [str(packaged), service, *arguments]
    python_service = "gpu-worker" if executable_name == "worker" else "backend"
    return [
        config.python(python_service),
        "-m",
        "local_app.service",
        service,
        *arguments,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Football-IQ local film room")
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=("start", "gui", "doctor", "install-model"),
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--database-url",
        help="Explicit existing development database; defaults to an owned native local database",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-worker", action="store_true")
    parser.add_argument("--skip-model-install", action="store_true")
    parser.add_argument(
        "--server-url",
        help="Process locally for an existing team API, including Cloudflare",
    )
    parser.add_argument(
        "--frontend-url", help="Team film-room URL to open in connected mode"
    )
    args = parser.parse_args()
    if args.action == "gui":
        from local_app.gui import run_gui

        return run_gui(args.data_dir)
    try:
        config = LocalConfig.load(args.data_dir, args.port, args.database_url)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"The local library could not open: {error}", file=sys.stderr)
        return 1
    environment = config.environment()
    environment["FOOTBALL_IQ_BUNDLE_ROOT"] = str(config.root)
    environment["PYTHONPATH"] = (
        str(config.root) + os.pathsep + environment.get("PYTHONPATH", "")
    )
    if args.action == "doctor":
        checks = {
            "static_ui": (config.root / "frontend" / "out" / "index.html").is_file(),
            "ffmpeg": bool(config.executable("ffmpeg")),
            "database": bool(config.external_database or config.executable("postgres")),
            "player_model": Path(environment["MODEL_DETECT_PATH"]).is_file(),
            "data_directory": str(config.data),
            "processing": "CPU",
        }
        print(json.dumps(checks, indent=2))
        return (
            0 if checks["static_ui"] and checks["ffmpeg"] and checks["database"] else 1
        )
    if args.action == "install-model":
        return subprocess.call(
            command(config, "install-detector"), cwd=config.root, env=environment
        )
    if args.server_url:
        from local_app.connected import run_connected

        return run_connected(
            config,
            args.server_url,
            args.frontend_url,
            args.no_browser,
            args.skip_model_install,
        )
    if not (config.root / "frontend" / "out" / "index.html").is_file():
        print(
            "The film-room UI has not been built. Source checkout: "
            "run npm ci and npm run build in frontend.",
            file=sys.stderr,
        )
        return 1
    supervisor = Supervisor(config.data, config.api_url)
    stopped = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)
    lock = InstanceLock(config.data)
    if not lock.acquire():
        from local_app.reopen import reopen

        return reopen(config, no_browser=args.no_browser)
    try:
        start_database(config, supervisor)
        snapshot_before_migration(config)
        # Migration output is private diagnostics; do not print connection strings.
        with (config.data / "logs" / "migrations.log").open("ab") as log:
            result = subprocess.run(
                command(config, "migrate"),
                cwd=config.root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        if result.returncode:
            raise RuntimeError(
                "Library migration failed. Existing files are preserved; see migration diagnostics."
            )
        record_migration(config)
        api = Service(
            "api",
            command(config, "api", "--port", str(config.api_port)),
            config.root,
            environment,
            health_url=config.api_url + "/ready",
        )
        supervisor.add(api)
        if not supervisor.start(api):
            raise RuntimeError("The local API could not start")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not stopped:
            if healthy(config.api_url + "/ready"):
                break
            if api.process and api.process.poll() is not None:
                raise RuntimeError(
                    "The local API exited during startup; see API diagnostics"
                )
            time.sleep(0.25)
        else:
            raise RuntimeError("The local API did not become ready")
        if not args.no_worker:
            supervisor.add(
                Service("worker", command(config, "worker"), config.root, environment)
            )
            supervisor.start(supervisor.services["worker"])
        if (
            not args.skip_model_install
            and not Path(environment["MODEL_DETECT_PATH"]).is_file()
        ):
            atomic_json(
                config.data / "runtime" / "controls" / "initial-model.json",
                {"action": "install-detector"},
            )
        print(f"Football-IQ is running at {config.api_url}")
        print(f"Library: {config.data}")
        if not args.no_browser:
            webbrowser.open(local_login_url(config))
        while not stopped:
            supervisor.controls(
                command(config, "install-detector"), config.root, environment
            )
            if supervisor.stop_requested:
                break
            supervisor.tick()
            time.sleep(2)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        supervisor.shutdown()
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
