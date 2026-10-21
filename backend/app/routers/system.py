"""Local/hosted processing capabilities and authenticated worker storage bridge."""

import json
import mimetypes
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user, require_admin, require_any_staff
from app.models import JobType, User, WorkerRuntime
from app.storage import (
    ARTIFACTS,
    CLIPS,
    OVERLAYS,
    RAW_VIDEO,
    active_storage_backend,
    generate_download_url_for_uri,
    get_s3_client,
    is_valid_key,
    local_object_path,
    local_storage_root,
    parse_storage_uri,
    physical_bucket,
    signed_local_url,
    upload_fileobj,
    verify_stream_signature,
)

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _default_profiles() -> list[Literal["review", "quick", "detailed"]]:
    return ["review", "quick"]


class WorkerRegistration(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    device: Literal["cpu", "cuda", "mps"] = "cpu"
    profiles: list[Literal["review", "quick", "detailed"]] = Field(
        default_factory=_default_profiles
    )
    models: dict[str, Any] = Field(default_factory=dict)
    job_types: list[JobType] | None = None
    metadata: dict[str, Any] | None = None


class WorkerHeartbeat(BaseModel):
    device: Literal["cpu", "cuda", "mps"] | None = None
    profiles: list[Literal["review", "quick", "detailed"]] | None = None
    models: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class RepairRequest(BaseModel):
    action: Literal["restart-worker", "restart-api", "install-detector", "clear-cache"]


class StorageResolve(BaseModel):
    uri: str


def _supervisor_status() -> dict[str, Any] | None:
    data_dir = get_settings().football_iq_data_dir
    if not data_dir:
        return None
    try:
        data = json.loads((Path(data_dir) / "runtime" / "status.json").read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


@router.get("/version")
async def system_version() -> dict[str, str]:
    """Deployed backend build, unauthenticated so a smoke check can compare it
    against the frontend's build SHA and flag a half-finished deploy."""
    return {"git_sha": os.environ.get("GIT_SHA", "unknown")}


@router.get("/errors")
async def recent_errors(
    _admin: Annotated[User, Depends(require_admin)],
) -> list[dict[str, Any]]:
    """The last 50 unhandled API errors since this server started, newest first."""
    from app.error_log import recent

    return recent()


@router.get("/release-inventory")
async def release_inventory(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    """Report observed versions; missing remote evidence stays unverified."""
    from app.models import ProcessingJob
    from app.services.metric_definitions import DEFINITION_VERSION

    schema = list(
        (await db.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    )
    workers = (await db.execute(select(WorkerRuntime))).scalars().all()
    jobs = (
        (
            await db.execute(
                select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(25)
            )
        )
        .scalars()
        .all()
    )
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "backend": {"git_sha": os.environ.get("GIT_SHA", "unknown")},
        "frontend": {
            "state": "unverified",
            "reason": "Compare the frontend build SHA at the deployed browser.",
        },
        "edge": {
            "state": "unverified",
            "reason": "Deployment connectivity and R2 bindings require a live trace.",
        },
        "database": {"schema_revisions": schema},
        "storage_backend": active_storage_backend(),
        "definition_version": DEFINITION_VERSION,
        "cfbd": {"configured": bool(get_settings().cfbd_api_key), "invocation_verified": False},
        "workers": [
            {
                "worker_id": worker.worker_id,
                "device": worker.device,
                "last_seen_at": worker.last_seen_at.isoformat(),
                "online": worker.last_seen_at >= datetime.now(UTC) - timedelta(seconds=90),
                "models": (worker.capabilities or {}).get("models", {}),
                "version": ((worker.capabilities or {}).get("metadata") or {}).get(
                    "git_sha", "unknown"
                ),
            }
            for worker in workers
        ],
        "recent_artifacts": [
            {
                "job_id": str(job.id),
                "status": str(job.status),
                "provenance": job.provenance,
                "generator_version": (job.output_artifacts or {}).get("generator_version"),
            }
            for job in jobs
        ],
        "production_verified": False,
    }


@router.get("/status")
async def system_status(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    backend = active_storage_backend()
    root = local_storage_root()
    writable = True
    free_bytes = None
    if backend == "local":
        try:
            root.mkdir(parents=True, exist_ok=True)
            writable = os.access(root, os.W_OK)
            free_bytes = shutil.disk_usage(root).free
        except OSError:
            writable = False
    now = datetime.now(UTC)
    rows = (
        (await db.execute(select(WorkerRuntime).order_by(WorkerRuntime.last_seen_at.desc())))
        .scalars()
        .all()
    )
    workers = []
    for row in rows:
        online = row.last_seen_at >= now - timedelta(seconds=90)
        cap = row.capabilities or {}
        workers.append(
            {
                "worker_id": row.worker_id,
                "device": row.device,
                "profiles": cap.get("profiles", []),
                "models": cap.get("models", {}),
                "last_seen_at": row.last_seen_at.isoformat(),
                "online": online,
                "processing_limits": (cap.get("metadata") or {}).get("processing_limits"),
            }
        )
    active = [w for w in workers if w["online"]]
    ready = writable and any(
        isinstance(w["models"].get("detect", w["models"].get("detector")), dict)
        and w["models"].get("detect", w["models"].get("detector", {})).get("ready")
        for w in active
    )
    supervisor = _supervisor_status()
    available_profiles = {p for w in active for p in w["profiles"]}
    review_ready = writable and "review" in available_profiles
    processing_ready = review_ready or (
        ready and bool(available_profiles.intersection({"quick", "detailed"}))
    )
    return {
        "mode": "local" if get_settings().football_iq_data_dir else "hosted",
        "state": "ready" if ready else "review_only",
        "storage": {
            "backend": backend,
            "root": str(root) if backend == "local" else None,
            "writable": writable,
            "free_bytes": free_bytes,
        },
        "worker": {
            "ready": ready,
            "workers": workers,
            "reason": None
            if ready
            else "No online worker with a ready detector. Film review remains available.",
        },
        "capabilities": {
            "film_review": writable,
            "processing": processing_ready,
            "calibrated_metrics": False,
            "training": False,
        },
        "supervisor": supervisor,
        "processing_limits": next(
            (worker["processing_limits"] for worker in active if worker["processing_limits"]),
            None,
        ),
        "profiles": [
            {
                "id": "review",
                "label": "Film review",
                "description": "Import, clip and annotate film without a model.",
                "available": review_ready,
            },
            {
                "id": "quick",
                "label": "Quick processing",
                "description": (
                    "Detection and tracking on the connected processor; "
                    "calibration is checked per film."
                ),
                "available": ready and "quick" in available_profiles,
            },
            {
                "id": "detailed",
                "label": "Detailed processing",
                "description": ("More analysis time; results stay experimental until reviewed."),
                "available": ready and "detailed" in available_profiles,
            },
        ],
        "limitations": [
            "Spatial metrics require reliable field calibration for each camera view.",
            "Player identity requires staff confirmation; model detections are review candidates.",
            "Automatic training and model promotion are disabled.",
        ],
    }


@router.post("/workers/register")
async def register_worker(
    body: WorkerRegistration,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    capabilities = body.model_dump(mode="json", exclude={"worker_id", "device"})
    if len(json.dumps(capabilities)) > 65536:
        raise HTTPException(413, "Worker capabilities too large")
    await db.execute(
        insert(WorkerRuntime)
        .values(
            worker_id=body.worker_id,
            device=body.device,
            capabilities=capabilities,
            registered_by=user.id,
            last_seen_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=[WorkerRuntime.worker_id],
            set_={
                "device": body.device,
                "capabilities": capabilities,
                "last_seen_at": datetime.now(UTC),
            },
        )
    )
    return {"worker_id": body.worker_id, "registered": True, "heartbeat_seconds": 30}


@router.post("/workers/{worker_id}/heartbeat")
async def worker_heartbeat(
    worker_id: str,
    body: WorkerHeartbeat,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    row = (
        await db.execute(
            select(WorkerRuntime).where(WorkerRuntime.worker_id == worker_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Worker not registered")
    changes = body.model_dump(mode="json", exclude_none=True)
    if len(json.dumps(changes)) > 65536:
        raise HTTPException(413, "Worker capabilities too large")
    if "device" in changes:
        row.device = changes.pop("device")
    row.capabilities = {**(row.capabilities or {}), **changes}
    row.last_seen_at = datetime.now(UTC)
    await db.flush()
    return {"worker_id": worker_id, "last_seen_at": row.last_seen_at.isoformat()}


@router.post("/repair", status_code=202)
async def request_repair(
    body: RepairRequest, _user: Annotated[User, Depends(require_admin)]
) -> dict[str, str]:
    data_dir = get_settings().football_iq_data_dir
    if not data_dir:
        raise HTTPException(409, "Repairs are managed by the local app supervisor")
    request_id = str(uuid.uuid4())
    controls = Path(data_dir) / "runtime" / "controls"
    controls.mkdir(parents=True, exist_ok=True)
    temporary = controls / f".{request_id}.tmp"
    temporary.write_text(json.dumps({"id": request_id, "action": body.action}))
    temporary.replace(controls / f"{request_id}.json")
    return {"id": request_id, "action": body.action, "state": "queued"}


def staff_storage_target(uri: str) -> tuple[str, str, str]:
    """``(scheme, logical bucket, key)`` for a staff-readable object URI."""
    if uri.startswith("library://"):
        uri = active_storage_backend() + "://" + uri[len("library://") :]
    scheme, bucket, key = parse_storage_uri(uri)
    if bucket not in {RAW_VIDEO, CLIPS, OVERLAYS, ARTIFACTS}:
        logical = next(
            (
                name
                for name in (RAW_VIDEO, CLIPS, OVERLAYS, ARTIFACTS)
                if physical_bucket(name) == bucket
            ),
            None,
        )
        if logical:
            bucket = logical
            uri = f"{scheme}://{bucket}/{key}"
    if bucket not in {RAW_VIDEO, CLIPS, OVERLAYS, ARTIFACTS} or not is_valid_key(key):
        raise ValueError("Unsupported storage object")
    return scheme, bucket, key


def staff_storage_url(uri: str) -> str:
    scheme, bucket, key = staff_storage_target(uri)
    uri = f"{scheme}://{bucket}/{key}"
    if scheme == "local" or get_settings().signed_url_mode == "worker":
        url = signed_local_url(bucket, key, get_settings().s3_presign_ttl).replace(
            "/api/v1/storage/", "/api/v1/system/storage/download/", 1
        )
        return f"{url}&scheme={scheme}"
    return generate_download_url_for_uri(uri)


@router.post("/storage/resolve")
async def resolve_storage(
    body: StorageResolve, _user: Annotated[User, Depends(require_any_staff)]
) -> dict[str, Any]:
    """A signed download URL, plus the object's size so the downloader can
    detect a stream that ended early (proxies drop Content-Length)."""
    try:
        target = staff_storage_target(body.uri)
        url = staff_storage_url(body.uri)
    except ValueError as exc:
        raise HTTPException(400, "Invalid storage URI") from exc
    size = await anyio.to_thread.run_sync(_object_size, *target)
    return {"download_url": url, "size_bytes": size}


def _object_size(scheme: str, bucket: str, key: str) -> int | None:
    """Best effort: the object's byte size, or ``None`` when it can't be read."""
    try:
        if scheme == "local":
            return local_object_path(bucket, key).stat().st_size
        head = get_s3_client().head_object(Bucket=physical_bucket(bucket), Key=key)
        return int(head["ContentLength"])
    except Exception:  # noqa: BLE001 - size is advisory; the download still works
        return None


@router.get("/storage/download/{bucket}/{key:path}")
async def stream_staff_artifact(
    bucket: str,
    key: str,
    request: Request,
    exp: str = Query(...),
    sig: str = Query(...),
    scheme: Literal["local", "s3"] | None = Query(default=None),
) -> Response:
    if (
        bucket not in {RAW_VIDEO, CLIPS, OVERLAYS, ARTIFACTS}
        or not is_valid_key(key)
        or not verify_stream_signature(bucket, key, exp, sig)
    ):
        raise HTTPException(403, "Invalid or expired object access")
    if (scheme or active_storage_backend()) == "s3":
        params = {"Bucket": physical_bucket(bucket), "Key": key}
        if request.headers.get("range"):
            params["Range"] = request.headers["range"]
        try:
            obj = await anyio.to_thread.run_sync(lambda: get_s3_client().get_object(**params))
        except Exception as exc:
            raise HTTPException(404, "Object not found or range unavailable") from exc
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "bytes",
        }
        for source, destination in (
            ("ContentLength", "Content-Length"),
            ("ContentRange", "Content-Range"),
            ("ETag", "ETag"),
        ):
            if source in obj:
                headers[destination] = str(obj[source])
        # Survives proxies that re-stream the body without Content-Length.
        total = str(obj.get("ContentRange", "")).rpartition("/")[2] or obj.get("ContentLength")
        if total not in (None, "", "*"):
            headers["X-Object-Size"] = str(total)
        return StreamingResponse(
            obj["Body"].iter_chunks(chunk_size=1024 * 1024),
            status_code=206 if "ContentRange" in obj else 200,
            media_type=obj.get("ContentType") or "application/octet-stream",
            headers=headers,
            background=BackgroundTask(obj["Body"].close),
        )
    path = local_object_path(bucket, key)
    if not path.is_file():
        raise HTTPException(404, "Object not found")
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Object-Size": str(path.stat().st_size),
        },
    )


@router.put("/storage/{bucket}/{key:path}", status_code=201)
async def store_worker_artifact(
    bucket: str, key: str, request: Request, _user: Annotated[User, Depends(require_any_staff)]
) -> dict[str, str]:
    if bucket not in {CLIPS, OVERLAYS, ARTIFACTS} or not is_valid_key(key):
        raise HTTPException(400, "Invalid artifact path")
    size = 0
    with tempfile.NamedTemporaryFile() as tmp:
        async for chunk in request.stream():
            size += len(chunk)
            if size > get_settings().max_upload_bytes:
                raise HTTPException(413, "Artifact exceeds upload limit")
            tmp.write(chunk)
        tmp.flush()
        tmp.seek(0)
        uri = await anyio.to_thread.run_sync(
            upload_fileobj,
            bucket,
            key,
            tmp,
            request.headers.get("content-type", "application/octet-stream"),
        )
    return {"uri": uri}
