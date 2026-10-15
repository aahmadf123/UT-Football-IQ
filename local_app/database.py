"""Own a native local PostgreSQL instance, or use an explicitly supplied database."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from local_app.config import LocalConfig, protect_private_path
from local_app.supervisor import Service, Supervisor


def database_environment(config: LocalConfig) -> dict[str, str]:
    env = config.environment()
    env.update(
        {
            "PGHOST": "127.0.0.1",
            "PGPORT": str(config.db_port),
            "PGUSER": "footiq",
            "PGPASSWORD": config.secrets["database_password"],
        }
    )
    native_libraries = [config.root / "native/postgres/lib", config.root / "native/lib"]
    if os.name != "nt":
        env["LD_LIBRARY_PATH"] = (
            os.pathsep.join(str(path) for path in native_libraries if path.is_dir())
            + os.pathsep
            + env.get("LD_LIBRARY_PATH", "")
        )
    return env


def sql(
    config: LocalConfig, query: str, database: str = "postgres"
) -> subprocess.CompletedProcess[str]:
    executable = config.executable("psql")
    if not executable:
        raise RuntimeError(
            "PostgreSQL tools are missing. Install the native Football-IQ package."
        )
    return subprocess.run(
        [
            executable,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--dbname",
            database,
            "--command",
            query,
        ],
        env=database_environment(config),
        capture_output=True,
        text=True,
        timeout=15,
    )


def start_database(config: LocalConfig, supervisor: Supervisor) -> None:
    if config.external_database:
        return
    postgres, initdb = config.executable("postgres"), config.executable("initdb")
    if not postgres or not initdb:
        raise RuntimeError(
            "Native PostgreSQL is unavailable. Use the packaged application, or pass "
            "--database-url to an existing development PostgreSQL database. "
            "Your Cloudflare library is not modified."
        )
    cluster = config.data / "postgres"
    if not (cluster / "PG_VERSION").is_file():
        cluster.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=config.data / "runtime", delete=False
        ) as stream:
            os.chmod(stream.name, 0o600)
            stream.write(config.secrets["database_password"])
            password_file = Path(stream.name)
        try:
            result = subprocess.run(
                [
                    initdb,
                    "--pgdata",
                    str(cluster),
                    "--username",
                    "footiq",
                    "--encoding",
                    "UTF8",
                    "--auth-host",
                    "scram-sha-256",
                    "--auth-local",
                    "scram-sha-256",
                    "--pwfile",
                    str(password_file),
                ],
                env=database_environment(config),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                (config.data / "logs/database-initialization.log").write_text(
                    result.stdout + "\n" + result.stderr, encoding="utf-8"
                )
                raise RuntimeError(
                    "Local database initialization failed. "
                    "Check the native PostgreSQL installation."
                )
        finally:
            password_file.unlink(missing_ok=True)
    command = [
        postgres,
        "-D",
        str(cluster),
        "-h",
        "127.0.0.1",
        "-p",
        str(config.db_port),
    ]
    if os.name != "nt":
        sockets = config.data / "runtime" / "postgres-sockets"
        sockets.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_path(sockets)
        command.extend(["-k", str(sockets)])
    pg_ctl = config.executable("pg_ctl")
    service = Service(
        "database",
        command,
        config.data,
        database_environment(config),
        stop_command=(
            [
                pg_ctl,
                "stop",
                "-D",
                str(cluster),
                "--mode",
                "fast",
                "--wait",
                "--timeout",
                "10",
            ]
            if pg_ctl
            else None
        ),
    )
    supervisor.add(service)
    if not supervisor.start(service):
        raise RuntimeError("Local database could not start")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if sql(config, "SELECT 1").returncode == 0:
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
        if service.process and service.process.poll() is not None:
            raise RuntimeError(
                "Local database exited during startup; see database diagnostics"
            )
        time.sleep(0.25)
    else:
        raise RuntimeError(
            "Local database did not become ready; see database diagnostics"
        )
    exists = sql(config, "SELECT 1 FROM pg_database WHERE datname='footiq'")
    if exists.stdout.strip() != "1":
        if sql(config, "CREATE DATABASE footiq").returncode:
            raise RuntimeError("Local library database could not be created")
    if sql(config, "CREATE EXTENSION IF NOT EXISTS vector", "footiq").returncode:
        raise RuntimeError("The local PostgreSQL installation is missing pgvector")
