"""Snapshot the owned local database before changing its schema."""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
from pathlib import Path

from local_app.config import LocalConfig, atomic_json
from local_app.database import database_environment, sql


def migration_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "backend/migrations/versions").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def snapshot(config: LocalConfig) -> Path | None:
    """Create a consistent pg_dump; original media remain in the storage folder."""
    if config.external_database:
        return None
    executable = config.executable("pg_dump")
    if not executable:
        raise RuntimeError(
            "Local database backup tool is missing; migration was not started"
        )
    directory = config.data / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    name = datetime.now(timezone.utc).strftime(
        "library-%Y%m%dT%H%M%SZ-"
    ) + secrets.token_hex(4)
    target = directory / (name + ".dump")
    temporary = target.with_suffix(".tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            result = subprocess.run(
                [
                    executable,
                    "--dbname",
                    "footiq",
                    "--format",
                    "custom",
                    "--no-owner",
                    "--no-privileges",
                ],
                env=database_environment(config),
                stdout=stream,
                stderr=subprocess.PIPE,
                timeout=180,
            )
            stream.flush()
            os.fsync(stream.fileno())
        if result.returncode or temporary.stat().st_size == 0:
            raise RuntimeError(
                "Local database backup failed; migration was not started"
            )
        temporary.replace(target)
        atomic_json(
            target.with_suffix(".json"),
            {
                "format": "postgresql-custom-v1",
                "database": "footiq",
                "media": "Originals are in the separate storage directory",
                "migration_fingerprint": migration_fingerprint(config.root),
            },
            private=True,
        )
        return target
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_before_migration(config: LocalConfig) -> None:
    if config.external_database:
        return
    marker = config.data / "runtime/migration-fingerprint.json"
    fingerprint = migration_fingerprint(config.root)
    if marker.is_file():
        import json

        if json.loads(marker.read_text()).get("fingerprint") == fingerprint:
            return
    # An empty newly initialized database has no coach work to protect.
    existing = sql(config, "SELECT to_regclass('public.alembic_version')", "footiq")
    if existing.returncode:
        raise RuntimeError(
            "Local schema could not be inspected; migration was not started"
        )
    if "alembic_version" in existing.stdout:
        snapshot(config)


def record_migration(config: LocalConfig) -> None:
    if not config.external_database:
        atomic_json(
            config.data / "runtime/migration-fingerprint.json",
            {"fingerprint": migration_fingerprint(config.root)},
            private=True,
        )
