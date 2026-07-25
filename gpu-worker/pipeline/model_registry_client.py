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


_lock = threading.Lock()
_resolved: dict[str, ResolvedModel | None] = {}


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
    if target.is_file():
        return target
    try:
        from pipeline import storage

        tmp = storage.download_to_temp(str(artifact_uri))
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

    Cached per process (one registry query + at most one download per
    model_type per worker lifetime); returns ``None`` whenever the bundled
    default weights should be used instead.
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
    """Test hook: clear the per-process resolution cache."""
    with _lock:
        _resolved.clear()
