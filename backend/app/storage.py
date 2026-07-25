"""Storage facade — S3-compatible object storage or local-filesystem backends.

URI schemes:
    ``s3://bucket/key``     — S3-compatible object store via boto3
    ``local://bucket/key``  — files under ``settings.local_storage_root``
                              (single-box / no-cloud deployments)

Writes go to the *active* backend (``settings.storage_backend``, or
auto-detected from object-store credentials). Reads and URL minting dispatch on the
**URI scheme**, so a database holding a mix of ``s3://`` and ``local://``
rows keeps working after a backend switch.

Local objects are served by ``app.routers.storage`` through HMAC-signed
URLs, because ``<video>`` tags cannot send Authorization headers.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config

from app.config import get_settings

_STREAM_KEY_CONTEXT = b"football-iq:stream-signing"

#: Buckets the download/streaming endpoints are allowed to serve.
DOWNLOADABLE_BUCKETS = frozenset({"raw-video", "clips", "overlays"})


# ── URI helpers ───────────────────────────────────────────────────────────────


def parse_storage_uri(uri: str) -> tuple[str, str, str]:
    """Split ``scheme://bucket/key`` into ``(scheme, bucket, key)``."""
    for scheme in ("s3", "local"):
        prefix = f"{scheme}://"
        if uri.startswith(prefix):
            rest = uri[len(prefix) :]
            if "/" not in rest:
                raise ValueError(f"Malformed {scheme}:// URI (no key): {uri!r}")
            bucket, key = rest.split("/", 1)
            if not bucket or not key:
                raise ValueError(f"Malformed {scheme}:// URI: {uri!r}")
            return scheme, bucket, key
    raise ValueError(f"Not a storage URI (expected s3:// or local://): {uri!r}")


def is_valid_key(key: str) -> bool:
    """Reject keys attempting path traversal (mirrors the Worker's isValidKey)."""
    if not key:
        return False
    if "\0" in key:
        return False
    if key.startswith("/"):
        return False
    return ".." not in key


def active_storage_backend() -> str:
    """Return the backend new writes go to: ``"s3"`` or ``"local"``."""
    settings = get_settings()
    if settings.storage_backend in ("s3", "local"):
        return settings.storage_backend
    configured = (
        settings.s3_endpoint_url and settings.s3_access_key_id and settings.s3_secret_access_key
    )
    return "s3" if configured else "local"


# ── Local backend ─────────────────────────────────────────────────────────────


def local_storage_root() -> Path:
    return Path(get_settings().local_storage_root).resolve()


def local_object_path(bucket: str, key: str) -> Path:
    """Resolve ``bucket/key`` under the local root, refusing traversal."""
    if not is_valid_key(key):
        raise ValueError(f"Invalid storage key: {key!r}")
    root = local_storage_root()
    path = (root / bucket / key).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Storage key escapes the local root: {key!r}")
    return path


# ── Signed local-stream URLs ─────────────────────────────────────────────────


def _derived_stream_key() -> bytes:
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, _STREAM_KEY_CONTEXT, hashlib.sha256).digest()


def sign_stream_path(bucket: str, key: str, exp: int) -> str:
    message = f"{bucket}:{key}:{exp}".encode()
    return hmac.new(_derived_stream_key(), message, hashlib.sha256).hexdigest()


def verify_stream_signature(bucket: str, key: str, exp: str, sig: str) -> bool:
    try:
        exp_num = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_num < int(time.time()):
        return False
    expected = sign_stream_path(bucket, key, exp_num)
    return hmac.compare_digest(expected, sig)


def signed_local_url(bucket: str, key: str, ttl: int) -> str:
    """Mint an HMAC-signed URL served by ``GET /api/v1/storage/{bucket}/{key}``."""
    settings = get_settings()
    exp = int(time.time()) + ttl
    sig = sign_stream_path(bucket, key, exp)
    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    base = settings.public_api_base_url.rstrip("/")
    return f"{base}/api/v1/storage/{quote(bucket, safe='')}/{encoded_key}?exp={exp}&sig={sig}"


# ── S3-compatible backend ────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_s3_client() -> Any:
    """Return a lazily-built boto3 S3 client configured from app settings.

    Memoised so the same client is reused for the process lifetime. Raises
    a clear error when object-store credentials are not configured.
    """
    settings = get_settings()
    if (
        not settings.s3_endpoint_url
        or not settings.s3_access_key_id
        or not settings.s3_secret_access_key
    ):
        raise RuntimeError(
            "Object storage is not configured. Set S3_ENDPOINT_URL, "
            "S3_ACCESS_KEY_ID, and S3_SECRET_ACCESS_KEY in the environment, "
            "or set STORAGE_BACKEND=local to use local-filesystem storage."
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


# ── Public write/read API ─────────────────────────────────────────────────────


def put_object(bucket: str, key: str, body: bytes, content_type: str) -> str:
    """Upload ``body`` to ``bucket/key`` on the active backend; return its URI."""
    if active_storage_backend() == "local":
        path = local_object_path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return f"local://{bucket}/{key}"
    client = get_s3_client()
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return f"s3://{bucket}/{key}"


def _use_signed_app_url(scheme: str) -> bool:
    """Whether to mint an HMAC-signed ``/api/v1/storage`` URL rather than presign.

    ``local://`` objects have no presigned form, so they always take the signed
    path. For ``s3://`` it is a deployment choice (``SIGNED_URL_MODE``):

    * ``worker`` — an edge Worker fronts the API and serves that route from R2.
      Preferred there: no object-store credentials end up in a browser-visible
      URL, and the Worker handles Range correctly, which is what makes ``<video>``
      scrubbing work rather than forcing a full download before a seek.
    * ``presigned``/``auto`` — hand out a normal S3 presigned URL.
    """
    if scheme == "local":
        return True
    return get_settings().signed_url_mode == "worker"


def generate_download_url(bucket: str, key: str, ttl: int | None = None) -> str:
    """Return a short-lived download URL for ``bucket/key`` on the active backend.

    Falls back to the configured ``S3_PRESIGN_TTL`` when ``ttl`` is not given.
    """
    settings = get_settings()
    expires = ttl if ttl is not None else settings.s3_presign_ttl
    if _use_signed_app_url(active_storage_backend()):
        return signed_local_url(bucket, key, expires)
    client = get_s3_client()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )
    return str(url)


def generate_download_url_for_uri(uri: str, ttl: int | None = None) -> str:
    """Mint a download URL for a full storage URI, dispatching on its scheme."""
    settings = get_settings()
    expires = ttl if ttl is not None else settings.s3_presign_ttl
    scheme, bucket, key = parse_storage_uri(uri)
    if _use_signed_app_url(scheme):
        return signed_local_url(bucket, key, expires)
    client = get_s3_client()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )
    return str(url)
