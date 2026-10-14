"""Model-registry serving leg — load promoted model artifacts, not just names.

Historically the registry was write-only from the worker's perspective:
training runs registered versions with an ``artifact_uri``, humans promoted
them, and then *nothing ever loaded them* — detectors always built from the
bundled ``{variant}.pt``. This module closes the loop:

    resolve("detect")  →  newest production ModelVersion of that type
                       →  artifact downloaded via the storage facade into a
                          version-keyed local cache
                       →  Path handed to the detector builder

Failure posture is offline-safe by design: no backend, no production
version, no artifact, or a failed download all return ``None`` and the
caller falls back to the bundled default weights. Promotion stays a human
decision (POST /api/v1/mlops/models/{id}/promote) — this is only the
serving half.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

_CACHE_ENV = "MODEL_CACHE_DIR"
_DEFAULT_CACHE = "~/.cache/football-iq/models"


@dataclass(frozen=True)
class ResolvedModel:
    model_version_id: str
    model_name: str
    version: str
    local_path: Path
    artifact_sha256: str | None = None
    validated: bool = False


_lock = threading.Lock()
_resolved: dict[str, ResolvedModel | None] = {}


def resolved_identity(model_types: tuple[str, ...] = ("detect", "ball", "coverage")) -> dict:
    """Snapshot served version ids and bytes for checkpoint invalidation."""
    from pipeline.runtime import model_provenance

    identity = {}
    for model_type in model_types:
        resolved = resolve(model_type)
        if resolved is not None:
            identity[model_type] = {
                "model_version_id": resolved.model_version_id,
                "model_sha256": model_provenance(
                    effective_variant=resolved.model_name, source="real",
                    model_path=str(resolved.local_path),
                )["model_sha256"],
            }
    return identity


def _cache_dir() -> Path:
    return Path(os.environ.get(_CACHE_ENV, _DEFAULT_CACHE)).expanduser()


def _auth_headers() -> dict[str, str]:
    try:
        from worker import auth as worker_auth

        bearer = worker_auth.token()
        if bearer:
            return {"Authorization": f"Bearer {bearer}"}
    except ImportError:
        pass
    return {}


def _fetch_production_version(model_type: str) -> dict | None:
    base = os.environ.get("BACKEND_API_URL", "")
    if not base:
        return None
    try:
        resp = httpx.get(
            f"{base.rstrip('/')}/api/v1/mlops/models",
            params={"model_type": model_type, "stage": "production", "limit": 1},
            headers=_auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        versions = resp.json()
        return dict(versions[0]) if versions else None
    except Exception as exc:
        log.warning("model_registry_query_failed", model_type=model_type, error=str(exc))
        return None


def _materialise(version: dict) -> Path | None:
    artifact_uri = version.get("artifact_uri")
    if not artifact_uri:
        log.warning(
            "model_version_has_no_artifact",
            model_version_id=version.get("id"),
            model_name=version.get("model_name"),
        )
        return None
    target = (
        _cache_dir()
        / str(version.get("model_name", "model"))
        / str(version.get("version", "v"))
        / Path(str(artifact_uri)).name
    )
    expected = (version.get("metrics") or {}).get("artifact_sha256")

    def approved_bytes(path: Path) -> bool:
        if not expected:
            return True  # Legacy models can serve experimentally, never validated.
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            log.warning("model_artifact_unreadable", model_version_id=version.get("id"), error=str(exc))
            return False
        return digest.hexdigest() == expected

    if target.is_file():
        if approved_bytes(target):
            return target
        log.warning("model_cached_artifact_hash_mismatch", model_version_id=version.get("id"))
    try:
        from pipeline import storage

        tmp = storage.download_to_temp(str(artifact_uri))
        if not approved_bytes(tmp):
            log.warning("model_artifact_hash_mismatch", model_version_id=version.get("id"))
            tmp.unlink(missing_ok=True)
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), target)
        log.info(
            "model_artifact_cached",
            model_name=version.get("model_name"),
            version=version.get("version"),
            path=str(target),
        )
        return target
    except Exception as exc:
        log.warning(
            "model_artifact_download_failed",
            artifact_uri=artifact_uri,
            error=str(exc),
        )
        return None


def resolve(model_type: str) -> ResolvedModel | None:
    """Resolve the production model artifact for a pipeline stage, if any.

    Cached within a job, reset at job boundaries so promotions/rollbacks take
    effect on the next run. Returns ``None`` when configured local defaults
    should be used instead; approved artifact bytes are checked before serving.
    """
    with _lock:
        if model_type in _resolved:
            return _resolved[model_type]

        result: ResolvedModel | None = None
        version = _fetch_production_version(model_type)
        if version is not None:
            path = _materialise(version)
            if path is not None:
                result = ResolvedModel(
                    model_version_id=str(version.get("id", "")),
                    model_name=str(version.get("model_name", "")),
                    version=str(version.get("version", "")),
                    local_path=path,
                    artifact_sha256=(version.get("metrics") or {}).get("artifact_sha256"),
                    validated=bool((version.get("metrics") or {}).get("artifact_sha256")),
                )
                log.info(
                    "model_registry_serving",
                    model_type=model_type,
                    model_name=result.model_name,
                    version=result.version,
                )
        _resolved[model_type] = result
        return result


def reset_cache() -> None:
    """Refresh promotions/rollbacks at the next job boundary; also used by tests."""
    with _lock:
        _resolved.clear()
