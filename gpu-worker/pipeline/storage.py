"""Storage facade for the GPU worker — R2 (S3-compatible) or local filesystem.

Accepted references:
    ``r2://bucket/key``     — Cloudflare R2 via boto3
    ``local://bucket/key``  — files under ``LOCAL_STORAGE_ROOT`` (shared with
                              the backend in single-box deployments)
    ``file:///abs/path``    — plain filesystem path
    bare key                — legacy form: resolved against the default
                              backend and the ``R2_BUCKET_NAME`` bucket

The active backend for *writes* comes from ``STORAGE_BACKEND`` (``r2`` |
``local``); when unset it is auto-detected (R2 credentials present → r2,
else local). *Reads* dispatch on the reference's scheme so mixed databases
keep working.

Uploads with bare keys are routed to a canonical bucket by prefix
(overlay/HLS renders → ``overlays``, everything else → ``artifacts``) so
cloud objects land in the buckets the Worker's ``/dl/*`` route can serve.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

R2_ENDPOINT = os.environ.get("R2_ENDPOINT_URL", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET_NAME", "football-iq")

LOCAL_STORAGE_ROOT = os.environ.get("LOCAL_STORAGE_ROOT", "./data/storage")

#: Bare-key upload prefixes that belong in the ``overlays`` bucket; all other
#: bare-key uploads go to ``artifacts``. (Raw video is never uploaded by the
#: worker — it arrives via the Worker/backend upload endpoints.)
_OVERLAY_PREFIXES = ("overlays/", "period_overlays/", "hls/")


def active_backend() -> str:
    """Return the backend new writes go to: ``"r2"`` or ``"local"``."""
    configured = os.environ.get("STORAGE_BACKEND", "")
    if configured in ("r2", "local"):
        return configured
    return "r2" if (R2_ENDPOINT and R2_ACCESS_KEY and R2_SECRET_KEY) else "local"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Split a storage reference into ``(scheme, bucket, key)``.

    Bare keys come back as ``("", default_bucket, key)``; ``file://`` paths
    as ``("file", "", path)``.
    """
    if ref.startswith("file://"):
        return "file", "", ref[len("file://") :]
    for scheme in ("r2", "local"):
        prefix = f"{scheme}://"
        if ref.startswith(prefix):
            rest = ref[len(prefix) :]
            if "/" not in rest:
                raise ValueError(f"Malformed {scheme}:// reference (no key): {ref!r}")
            bucket, key = rest.split("/", 1)
            return scheme, bucket, key
    return "", R2_BUCKET, ref


def bucket_for_key(key: str) -> str:
    """Canonical bucket for a bare upload key."""
    if key.startswith(_OVERLAY_PREFIXES):
        return "overlays"
    return "artifacts"


def local_object_path(bucket: str, key: str) -> Path:
    root = Path(LOCAL_STORAGE_ROOT).resolve()
    path = (root / bucket / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Storage key escapes the local root: {key!r}")
    return path


def _s3_client() -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
    )


def _record(operation: str, bucket: str, outcome: str, duration: float) -> None:
    """Best-effort metrics emission — never raises."""
    try:
        from worker.observability import record_r2_operation

        record_r2_operation(operation, bucket, outcome, duration)
    except Exception:
        pass


# ── Downloads ─────────────────────────────────────────────────────────────────


def download_to_temp(ref: str) -> Path:
    """Materialise a storage reference as a local temporary file.

    Always returns a caller-owned copy (safe to unlink), even for local
    backends — pipeline stages treat the returned path as disposable.
    """
    raw = str(ref)
    if "://" not in raw:
        direct = Path(raw)
        if direct.is_file():
            return _copy_to_temp(direct, label=raw)
    scheme, bucket, key = parse_ref(raw)

    if scheme == "file":
        return _copy_to_temp(Path(key), label=ref)

    if scheme == "local" or (scheme == "" and active_backend() == "local"):
        return _copy_to_temp(local_object_path(bucket, key), label=ref)

    suffix = Path(key).suffix or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    start = time.monotonic()
    try:
        log.info("storage_download_start", key=key, bucket=bucket)
        _s3_client().download_fileobj(bucket, key, tmp)
        tmp.flush()
        duration = time.monotonic() - start
        _record("download", bucket, "success", duration)
        log.info("storage_download_done", key=key, path=tmp.name,
                 duration_seconds=round(duration, 3))
        return Path(tmp.name)
    except Exception as exc:
        duration = time.monotonic() - start
        _record("download", bucket, "error", duration)
        log.error("storage_download_failed", key=key, bucket=bucket,
                  error=str(exc), duration_seconds=round(duration, 3))
        raise
    finally:
        tmp.close()


def _copy_to_temp(source: Path, label: str) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"Storage object not found: {label} ({source})")
    suffix = source.suffix or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    shutil.copy2(source, tmp.name)
    log.info("storage_local_copy", source=str(source), path=tmp.name)
    return Path(tmp.name)


def resolve_readable_path(ref: str) -> Path | None:
    """Return a directly-readable filesystem path for ``ref`` when one exists.

    Lets callers (e.g. video sources) avoid a temp copy for file:// and
    local:// references. Returns ``None`` for remote references.
    """
    raw = str(ref)
    if "://" not in raw:
        direct = Path(raw)
        if direct.is_file():
            return direct
    scheme, bucket, key = parse_ref(raw)
    if scheme == "file":
        return Path(key)
    if scheme == "local" or (scheme == "" and active_backend() == "local"):
        return local_object_path(bucket, key)
    return None


# ── Uploads ───────────────────────────────────────────────────────────────────


def upload_file(
    local_path: Path,
    key: str,
    content_type: str = "application/octet-stream",
    bucket: str | None = None,
) -> str:
    """Store a local file under ``bucket/key``; return its storage URI."""
    target_bucket = bucket or bucket_for_key(key)

    if active_backend() == "local":
        dest = local_object_path(target_bucket, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        uri = f"local://{target_bucket}/{key}"
        log.info("storage_upload_done", key=key, uri=uri, backend="local")
        return uri

    start = time.monotonic()
    try:
        log.info("storage_upload_start", key=key, path=str(local_path), bucket=target_bucket)
        with Path(local_path).open("rb") as fh:
            _s3_client().upload_fileobj(
                fh,
                target_bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        uri = f"r2://{target_bucket}/{key}"
        duration = time.monotonic() - start
        _record("upload", target_bucket, "success", duration)
        log.info("storage_upload_done", key=key, uri=uri,
                 duration_seconds=round(duration, 3))
        return uri
    except Exception as exc:
        duration = time.monotonic() - start
        _record("upload", target_bucket, "error", duration)
        log.error("storage_upload_failed", key=key, bucket=target_bucket,
                  error=str(exc), duration_seconds=round(duration, 3))
        raise


def upload_bytes(
    data: bytes,
    key: str,
    content_type: str = "application/octet-stream",
    bucket: str | None = None,
) -> str:
    """Store raw bytes under ``bucket/key``; return its storage URI."""
    target_bucket = bucket or bucket_for_key(key)

    if active_backend() == "local":
        dest = local_object_path(target_bucket, key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        uri = f"local://{target_bucket}/{key}"
        log.info("storage_upload_bytes_done", key=key, uri=uri, backend="local")
        return uri

    start = time.monotonic()
    try:
        log.info("storage_upload_bytes_start", key=key, size=len(data), bucket=target_bucket)
        _s3_client().upload_fileobj(
            io.BytesIO(data),
            target_bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        uri = f"r2://{target_bucket}/{key}"
        duration = time.monotonic() - start
        _record("upload_bytes", target_bucket, "success", duration)
        log.info("storage_upload_bytes_done", key=key, uri=uri,
                 duration_seconds=round(duration, 3))
        return uri
    except Exception as exc:
        duration = time.monotonic() - start
        _record("upload_bytes", target_bucket, "error", duration)
        log.error("storage_upload_bytes_failed", key=key, bucket=target_bucket,
                  error=str(exc), duration_seconds=round(duration, 3))
        raise
