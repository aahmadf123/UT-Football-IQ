"""Storage router — serves local-backend objects through HMAC-signed URLs.

This is the local-mode counterpart of the Cloudflare Worker's ``/dl/*``
streaming route: ``<video>`` tags cannot send Authorization headers, so
access is granted by a short-lived HMAC signature minted by
:func:`app.storage.signed_local_url` instead of a Bearer token.

Starlette's ``FileResponse`` handles Range requests natively, which the
clip-review player needs for seeking.
"""

import mimetypes

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from app.storage import (
    DOWNLOADABLE_BUCKETS,
    is_valid_key,
    local_object_path,
    verify_stream_signature,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/storage", tags=["storage"])


@router.get("/{bucket}/{key:path}")
async def stream_object(
    bucket: str,
    key: str,
    exp: str = Query(...),
    sig: str = Query(...),
) -> FileResponse:
    """Stream a locally-stored object after verifying its signed URL."""
    if bucket not in DOWNLOADABLE_BUCKETS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"Bucket not allowed: {bucket}"
        )
    if not is_valid_key(key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid key")
    if not verify_stream_signature(bucket, key, exp, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or expired signature"
        )

    try:
        path = local_object_path(bucket, key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid key") from exc
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")

    media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
