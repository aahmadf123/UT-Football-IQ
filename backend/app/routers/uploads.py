"""Uploads router — Worker-parity upload/download endpoints on the backend.

Mirrors the Cloudflare Worker's edge contract exactly, so the frontend uses
one code path in both deployments (``workerBase()`` falls back to the API
base when ``NEXT_PUBLIC_WORKER_URL`` is unset):

    POST /api/v1/videos/upload-url         -> { uploadUrl, key }
    PUT  /api/v1/videos/upload/{key}       -> { key, size, etag, storageUri }
    GET  /api/v1/videos/download-url       -> { downloadUrl }

The response field names are camelCase on purpose — they are the Worker's
wire shapes (see workers/src/index.ts), not backend conventions.

NOTE: this router must be registered BEFORE the videos router so the
literal ``/download-url`` path wins over ``/{video_id}``.
"""

import hashlib
import re
import time
from typing import Annotated
from urllib.parse import quote

import structlog
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.config import get_settings
from app.deps import get_current_user, require_any_staff
from app.models import User
from app.storage import (
    DOWNLOADABLE_BUCKETS,
    active_storage_backend,
    generate_download_url,
    get_r2_client,
    is_valid_key,
    local_object_path,
)

# If PUBLIC_API_BASE_URL still points to localhost in production, derive the
# public base from the incoming request so minted upload URLs are reachable.
_LOCALHOST_BASE_RE = re.compile(
    r"^https?://(?:localhost|127\.\d+\.\d+\.\d+)(?::\d+)?$",
    re.IGNORECASE,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/videos", tags=["uploads"])

_RAW_BUCKET = "raw-video"


class UploadUrlRequest(BaseModel):
    filename: str


def _reject_oversize(size: int) -> None:
    limit = get_settings().max_upload_bytes
    if size > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds the {limit} byte limit",
        )


def _upload_base(request: Request) -> str:
    """Return the public base URL for upload/signing, falling back to the request origin."""
    configured = get_settings().public_api_base_url.rstrip("/")
    if configured and not _LOCALHOST_BASE_RE.match(configured):
        return configured

    # When running behind a reverse proxy (Fly), uvicorn may see `http` unless
    # proxy headers are honored. Prefer forwarded headers so minted URLs match
    # the public origin.
    xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    xf_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    proto = xf_proto or request.url.scheme
    host = xf_host or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


@router.post("/upload-url")
async def create_upload_url(
    body: UploadUrlRequest,
    request: Request,
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, str]:
    """Mint an upload key + proxy URL (Worker parity)."""
    if not body.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="filename is required")
    # Worker uses `raw/${Date.now()}-${filename}`; mirror it. The filename is
    # embedded in the key, so strip path separators and traversal sequences.
    safe_name = body.filename.replace("\\", "/").split("/")[-1].replace("..", "_") or "upload.bin"
    key = f"raw/{int(time.time() * 1000)}-{safe_name}"
    base = _upload_base(request)
    upload_url = f"{base}/api/v1/videos/upload/{quote(key, safe='')}"
    return {"uploadUrl": upload_url, "key": key}


@router.put("/upload/{key:path}", status_code=status.HTTP_201_CREATED)
async def upload_object(
    key: str,
    request: Request,
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, object]:
    """Stream the request body into raw-video storage (Worker parity).

    Guardrails: staff-only (matching video registration), a byte ceiling
    (``max_upload_bytes`` — checked against Content-Length up front and
    re-enforced while streaming, since the header can lie), and no
    overwrite of an existing object (keys are minted per-upload; a PUT to
    a taken key is a mistake or an attempt to clobber someone's film).
    """
    if not key or not is_valid_key(key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing upload key")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        _reject_oversize(int(declared))

    md5 = hashlib.md5(usedforsecurity=False)
    size = 0

    if active_storage_backend() == "local":
        path = local_object_path(_RAW_BUCKET, key)
        if path.exists():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Object already exists"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    _reject_oversize(size)
                    out.write(chunk)
                    md5.update(chunk)
        except BaseException:
            path.unlink(missing_ok=True)  # no partial objects
            raise
        storage_uri = f"local://{_RAW_BUCKET}/{key}"
    else:
        import tempfile

        client = get_r2_client()
        exists = await to_thread.run_sync(_r2_object_exists, client, key)
        if exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Object already exists"
            )
        content_type = request.headers.get("content-type", "video/mp4")
        with tempfile.NamedTemporaryFile() as tmp:
            async for chunk in request.stream():
                size += len(chunk)
                _reject_oversize(size)
                tmp.write(chunk)
                md5.update(chunk)
            tmp.flush()
            tmp.seek(0)
            await to_thread.run_sync(
                lambda: client.upload_fileobj(
                    tmp, _RAW_BUCKET, key, ExtraArgs={"ContentType": content_type}
                )
            )
        storage_uri = f"r2://{_RAW_BUCKET}/{key}"

    log.info("upload_stored", key=key, size=size, backend=active_storage_backend())
    return {"key": key, "size": size, "etag": md5.hexdigest(), "storageUri": storage_uri}


def _r2_object_exists(client: object, key: str) -> bool:
    try:
        client.head_object(Bucket=_RAW_BUCKET, Key=key)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


@router.get("/download-url")
async def create_download_url(
    _current_user: Annotated[User, Depends(get_current_user)],
    bucket: str = Query(...),
    key: str = Query(...),
) -> dict[str, str]:
    """Mint a short-lived streaming URL for ``bucket/key`` (Worker parity)."""
    if bucket not in DOWNLOADABLE_BUCKETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Bucket not allowed: {bucket}"
        )
    if not is_valid_key(key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid key")
    try:
        download_url = generate_download_url(bucket, key)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"downloadUrl": download_url}
