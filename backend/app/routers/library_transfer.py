"""Manual, portable session transfer between independently installed libraries."""

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import get_db
from app.deps import require_analyst_or_above
from app.models import User
from app.services.library_transfer import (
    build_archive,
    import_archive,
    session_graph,
    validate_archive,
)
from app.storage import ARTIFACTS, local_object_path, sign_stream_path, verify_stream_signature

router = APIRouter(prefix="/api/v1/library", tags=["library-transfer"])


@router.post("/sessions/{session_id}/export")
async def export_session(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _actor: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    async with AsyncSession(bind=db.bind) as snapshot:
        await snapshot.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        graph = await session_graph(snapshot, session_id)
    try:
        result = await run_in_threadpool(build_archive, graph, session_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            409,
            "Could not export the complete session. "
            "Check that saved recording and evidence files are available.",
        ) from exc
    key = f"session-exports/{result['archive_id']}.zip"
    expiry = int(time.time()) + 3600
    signature = sign_stream_path(ARTIFACTS, key, expiry)
    base = get_settings().public_api_base_url.rstrip("/")
    result["download_url"] = (
        f"{base}/api/v1/library/exports/{result['archive_id']}?exp={expiry}&sig={signature}"
    )
    return result


@router.get("/exports/{archive_id}")
async def download_archive(
    archive_id: uuid.UUID, exp: Annotated[str, Query()], sig: Annotated[str, Query()]
) -> FileResponse:
    key = f"session-exports/{archive_id}.zip"
    if not verify_stream_signature(ARTIFACTS, key, exp, sig):
        raise HTTPException(403, "This archive download link expired or is invalid.")
    path = local_object_path(ARTIFACTS, key)
    if not path.is_file():
        raise HTTPException(404, "Archive not found. Export the session again.")
    return FileResponse(
        path, media_type="application/zip", filename=f"toledo-session-{archive_id}.fiq.zip"
    )


@router.post("/import")
async def import_session(
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_analyst_or_above)],
    dry_run: Annotated[bool, Form()] = True,
) -> dict[str, Any]:
    manifest = await run_in_threadpool(validate_archive, file.file)
    return await import_archive(db, file.file, manifest, actor.id, dry_run)
