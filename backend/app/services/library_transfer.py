"""Portable session archives: immutable media, stable IDs, append-only import.

Accounts, credentials, running jobs, model weights, and global health data are
not portable session content. Original annotation attribution and external
lineage stay in archive metadata; imported rows do not create login accounts.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from enum import Enum as PythonEnum
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from fastapi import HTTPException
from sqlalchemy import Date, DateTime, Enum, inspect, or_, select, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Base
from app.models import (
    Clip,
    ClipEdit,
    ClipPlayer,
    CoachCorrection,
    Event,
    FieldCalibration,
    FootballSession,
    HeadOrientationReview,
    Label,
    Metric,
    Play,
    Player,
    PlayerVisibilityState,
    PlayView,
    PoseKeypoints,
    RosterAssignment,
    Tracklet,
    TrackPoint,
    Video,
)
from app.storage import (
    ARTIFACTS,
    CLIPS,
    OVERLAYS,
    RAW_VIDEO,
    get_s3_client,
    local_object_path,
    parse_storage_uri,
    physical_bucket,
)

FORMAT = "football-iq-session"
VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024 * 1024
MAX_RECORDS = 500_000
BUCKETS = {RAW_VIDEO, CLIPS, OVERLAYS, ARTIFACTS}
MODELS: list[type[Base]] = [
    Player,
    FootballSession,
    Video,
    FieldCalibration,
    Clip,
    Play,
    PlayView,
    Tracklet,
    TrackPoint,
    Event,
    Label,
    CoachCorrection,
    Metric,
    PoseKeypoints,
    HeadOrientationReview,
    ClipPlayer,
    ClipEdit,
    RosterAssignment,
]
TABLES = {model.__tablename__: model for model in MODELS}
OPTIONAL_TABLES = {"clip_edits", "roster_assignments"}
MEDIA_FIELDS = {"storage_uri", "evidence_uri"}
EXTERNAL_TABLES = {"users", "processing_jobs", "model_versions", "training_datasets"}
PRIVATE_KEY = re.compile(r"password|secret|token|credential|authorization|access_key", re.I)


def sanitize(value: Any) -> Any:
    """Keep coaching data while excluding secrets and machine-specific paths."""
    if isinstance(value, dict):
        return {
            key: sanitize(item)
            for key, item in value.items()
            if not PRIVATE_KEY.search(key) and not key.endswith("_path")
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, PythonEnum)):
        return str(value)
    if isinstance(value, str) and (
        value.startswith(("/", "file://", "local://", "s3://"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        return None
    return value


def record(row: Base) -> dict[str, Any]:
    mapper = inspect(type(row))
    return {attr.key: getattr(row, attr.key) for attr in mapper.column_attrs}


async def rows_for[Row: Base](
    db: AsyncSession, model: type[Row], field: str, ids: list[uuid.UUID], *, lock: bool = False
) -> list[Row]:
    rows: list[Row] = []
    for start in range(0, len(ids), 2000):
        query = select(model).where(getattr(model, field).in_(ids[start : start + 2000]))
        result = await db.execute(query.with_for_update() if lock else query)
        rows.extend(result.scalars().all())
    return rows


async def session_graph(db: AsyncSession, session_id: uuid.UUID) -> dict[str, list[dict[str, Any]]]:
    session = await db.get(FootballSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    videos = await rows_for(db, Video, "practice_session_id", [session_id])
    video_ids = [row.id for row in videos]
    clips = await rows_for(db, Clip, "video_id", video_ids)
    clip_ids = [row.id for row in clips]
    tracks = await rows_for(db, Tracklet, "clip_id", clip_ids)
    track_ids = [row.id for row in tracks]
    metrics = await rows_for(db, Metric, "clip_id", clip_ids)
    links = await rows_for(db, ClipPlayer, "clip_id", clip_ids)
    player_ids = list(
        {
            value
            for value in [row.player_id for row in tracks] + [row.player_id for row in links]
            if value is not None
        }
    )
    plays = await rows_for(db, Play, "session_id", [session_id])
    graph: dict[str, Sequence[Base]] = {
        "football_sessions": [session],
        "videos": videos,
        "clips": clips,
        "players": await rows_for(db, Player, "id", player_ids),
        "tracklets": tracks,
        "track_points": await rows_for(db, TrackPoint, "tracklet_id", track_ids),
        "field_calibrations": await rows_for(db, FieldCalibration, "video_id", video_ids),
        "events": await rows_for(db, Event, "clip_id", clip_ids),
        "labels": await rows_for(db, Label, "clip_id", clip_ids),
        "coach_corrections": await rows_for(db, CoachCorrection, "clip_id", clip_ids),
        "metrics": metrics,
        "clip_players": links,
        "plays": plays,
        "play_views": await rows_for(db, PlayView, "play_id", [row.id for row in plays]),
        "pose_keypoints": await rows_for(db, PoseKeypoints, "tracklet_id", track_ids),
        "head_orientation_reviews": await rows_for(
            db, HeadOrientationReview, "metric_id", [row.id for row in metrics]
        ),
        "clip_edits": await rows_for(db, ClipEdit, "video_id", video_ids),
        "roster_assignments": (
            await db.execute(
                select(RosterAssignment).where(
                    RosterAssignment.player_id.in_(player_ids),
                    or_(
                        RosterAssignment.session_id.is_(None),
                        RosterAssignment.session_id == session_id,
                    ),
                )
            )
        )
        .scalars()
        .all(),
    }
    if sum(len(rows) for rows in graph.values()) > MAX_RECORDS:
        raise HTTPException(413, "This session exceeds the portable archive record limit.")
    return {name: [record(row) for row in rows] for name, rows in graph.items()}


@contextmanager
def read_media(uri: str) -> Iterator[BinaryIO]:
    scheme, bucket, key = parse_storage_uri(uri)
    if bucket not in BUCKETS:
        raise ValueError("Unsupported session media bucket")
    if scheme == "local":
        with local_object_path(bucket, key).open("rb") as file:
            yield file
    else:
        body = get_s3_client().get_object(Bucket=physical_bucket(bucket), Key=key)["Body"]
        try:
            yield body
        finally:
            body.close()


def build_archive(graph: dict[str, list[dict[str, Any]]], session_id: uuid.UUID) -> dict[str, Any]:
    archive_id = uuid.uuid4()
    path = local_object_path(ARTIFACTS, f"session-exports/{archive_id}.zip")
    path.parent.mkdir(parents=True, exist_ok=True)
    expiry = datetime.now(UTC).timestamp() - 24 * 3600
    for old in path.parent.glob("*.zip"):
        if old.stat().st_mtime < expiry:
            old.unlink(missing_ok=True)
    media: list[dict[str, Any]] = []
    source_media: dict[str, str] = {}
    total = 0
    try:
        with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
            for table, rows in graph.items():
                for row in rows:
                    if table == "videos":
                        row["filename"] = str(row["filename"]).replace("\\", "/").rsplit("/", 1)[-1]
                    for field in MEDIA_FIELDS:
                        uri = row.get(field)
                        if not uri:
                            continue
                        if uri not in source_media:
                            _, bucket, key = parse_storage_uri(uri)
                            suffix = Path(key).suffix.lower()
                            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                                suffix = ".bin"
                            member = f"media/{uuid.uuid4()}{suffix}"
                            digest = hashlib.sha256()
                            size = 0
                            with (
                                read_media(uri) as source,
                                archive.open(member, "w", force_zip64=True) as dest,
                            ):
                                while chunk := source.read(1024 * 1024):
                                    size += len(chunk)
                                    total += len(chunk)
                                    if total > MAX_ARCHIVE_BYTES:
                                        raise HTTPException(
                                            413,
                                            "Session exceeds the 100 GB archive limit.",
                                        )
                                    digest.update(chunk)
                                    dest.write(chunk)
                            media.append(
                                {
                                    "member": member,
                                    "bucket": bucket,
                                    "sha256": digest.hexdigest(),
                                    "size_bytes": size,
                                }
                            )
                            source_media[uri] = member
                        row[field] = {"archive_media": source_media[uri]}
            manifest = {
                "format": FORMAT,
                "version": VERSION,
                "session_id": str(session_id),
                "exported_at": datetime.now(UTC).isoformat(),
                "entities": sanitize(graph),
                "media": media,
                "exclusions": [
                    "login accounts and credentials",
                    "running jobs",
                    "model weights",
                    "global health data and reports",
                ],
            }
            payload = json.dumps(manifest, allow_nan=False, separators=(",", ":")).encode()
            if len(payload) > MAX_MANIFEST_BYTES:
                raise HTTPException(
                    413, "This session exceeds the portable archive manifest limit."
                )
            archive.writestr("manifest.json", payload, compress_type=zipfile.ZIP_DEFLATED)
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        return {
            "archive_id": str(archive_id),
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    except Exception:
        path.unlink(missing_ok=True)
        raise


def validate_archive(file: BinaryIO) -> dict[str, Any]:
    """Validate before any DB or storage writes; never extract archive paths."""
    file.seek(0)
    try:
        with zipfile.ZipFile(file) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate archive members")
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in info.filename
                    or info.flag_bits & 1
                ):
                    raise ValueError("Unsafe or encrypted archive member")
            info = archive.getinfo("manifest.json")
            if info.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("Manifest exceeds the 64 MB limit")
            decoded = json.loads(archive.read(info))
            if not isinstance(decoded, dict):
                raise ValueError("Manifest must be an object")
            manifest: dict[str, Any] = decoded
            if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
                raise ValueError("Unsupported Football-IQ archive version")
            session_id = str(uuid.UUID(manifest["session_id"]))
            entities = manifest["entities"]
            if (
                not isinstance(entities, dict)
                or set(entities) - set(TABLES)
                or (set(TABLES) - set(entities)) - OPTIONAL_TABLES
            ):
                raise ValueError("Archive entity tables do not match its version")
            if (
                len(entities["football_sessions"]) != 1
                or entities["football_sessions"][0]["id"] != session_id
            ):
                raise ValueError("Archive must contain exactly its declared session")
            if sum(len(rows) for rows in entities.values()) > MAX_RECORDS:
                raise ValueError("Archive has too many records")
            expected = {"manifest.json"}
            total = 0
            for media in manifest["media"]:
                member = media["member"]
                if (
                    not re.fullmatch(r"media/[0-9a-f-]{36}\.[a-z0-9]{1,10}", member)
                    or member in expected
                    or media["bucket"] not in BUCKETS
                ):
                    raise ValueError("Invalid media manifest")
                expected.add(member)
                info = archive.getinfo(member)
                total += info.file_size
                if total > MAX_ARCHIVE_BYTES or info.file_size != media["size_bytes"]:
                    raise ValueError("Media size does not match the manifest")
                digest = hashlib.sha256()
                with archive.open(info) as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != media["sha256"]:
                    raise ValueError("Media checksum does not match the manifest")
            if set(names) != expected:
                raise ValueError("Archive contains unlisted files")
            # Make all graph references explicit; package imports cannot attach to unrelated rows.
            identifiers: dict[str, set[str]] = {}
            for table, rows in entities.items():
                if not isinstance(rows, list):
                    raise ValueError("Entity rows must be lists")
                model = TABLES[table]
                keys = {attr.key for attr in inspect(model).column_attrs}
                seen: set[str] = set()
                for row in rows:
                    if not isinstance(row, dict) or set(row) != keys:
                        raise ValueError("Entity fields do not match this archive version")
                    identity = str(row.get("id", f"{row.get('clip_id')}:{row.get('player_id')}"))
                    if identity in seen:
                        raise ValueError("Duplicate entity IDs")
                    seen.add(identity)
                    for attr in inspect(model).column_attrs:
                        column = attr.columns[0]
                        if isinstance(column.type, UUID) and row.get(attr.key) is not None:
                            uuid.UUID(row[attr.key])
                    for field in MEDIA_FIELDS:
                        if row.get(field) is not None and (
                            not isinstance(row[field], dict)
                            or row[field].get("archive_media") not in expected
                        ):
                            raise ValueError("Media references must use manifest members")
                identifiers[table] = seen
            for table, rows in entities.items():
                for attr in inspect(TABLES[table]).column_attrs:
                    for fk in attr.columns[0].foreign_keys:
                        target = fk.column.table.name
                        if target in EXTERNAL_TABLES:
                            continue
                        for row in rows:
                            value = row.get(attr.key)
                            if value is not None and str(value) not in identifiers.get(
                                target, set()
                            ):
                                raise ValueError("Entity references an object outside this session")
            return manifest
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise HTTPException(422, f"Invalid session archive: {exc}") from exc


def imported_record(
    model: type[Base], row: dict[str, Any], actor_id: uuid.UUID, media: dict[str, str]
) -> dict[str, Any]:
    values = dict(row)
    lineage: dict[str, Any] = {}
    for attr in inspect(model).column_attrs:
        column = attr.columns[0]
        value = values.get(attr.key)
        if attr.key in MEDIA_FIELDS:
            values[attr.key] = media[value["archive_media"]] if value else None
            continue
        external = any(fk.column.table.name in EXTERNAL_TABLES for fk in column.foreign_keys)
        if external:
            if value is not None:
                lineage[attr.key] = value
            values[attr.key] = str(actor_id) if not column.nullable else None
            value = values[attr.key]
        if value is not None:
            if isinstance(column.type, UUID):
                values[attr.key] = uuid.UUID(value)
            elif isinstance(column.type, DateTime):
                values[attr.key] = datetime.fromisoformat(value)
            elif isinstance(column.type, Date):
                values[attr.key] = date.fromisoformat(value)
            elif isinstance(column.type, Enum) and column.type.enum_class:
                values[attr.key] = column.type.enum_class(value)
    if lineage:
        for field in (
            "provenance",
            "original_value",
            "label_value",
            "attributes",
            "metadata_",
            "before_state",
        ):
            if field in values:
                values[field] = {**(values[field] or {}), "_archive_lineage": lineage}
                break
    if model is Player:
        values["visibility_state"] = PlayerVisibilityState.staff_only
        values["visibility_updated_by"] = None
    return values


def comparable(model: type[Base], row: dict[str, Any]) -> dict[str, Any]:
    values = dict(row)
    for attr in inspect(model).column_attrs:
        if any(fk.column.table.name in EXTERNAL_TABLES for fk in attr.columns[0].foreign_keys):
            values.pop(attr.key, None)
    for field in (
        "provenance",
        "original_value",
        "label_value",
        "attributes",
        "metadata_",
        "before_state",
    ):
        if isinstance(values.get(field), dict):
            values[field] = {
                key: item for key, item in values[field].items() if key != "_archive_lineage"
            }
            if not values[field]:
                values[field] = None
    if model is Player:
        for field in ("visibility_state", "visibility_updated_by", "visibility_updated_at"):
            values.pop(field, None)
    result: dict[str, Any] = sanitize(values)
    for field in MEDIA_FIELDS:
        if field in values:
            result[field] = values[field]
    return result


def write_import_media(file: BinaryIO, manifest: dict[str, Any]) -> dict[str, str]:
    destinations: dict[str, str] = {}
    file.seek(0)
    with zipfile.ZipFile(file) as archive:
        for media in manifest["media"]:
            suffix = Path(media["member"]).suffix
            key = f"imports/{media['sha256']}{suffix}"
            path = local_object_path(media["bucket"], key)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                # Atomic publication avoids exposing a partial video to the player.
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                        temporary = Path(temp.name)
                        with archive.open(media["member"]) as source:
                            shutil.copyfileobj(source, temp, 1024 * 1024)
                        temp.flush()
                    temporary.replace(path)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
            else:
                digest = hashlib.sha256()
                with path.open("rb") as saved:
                    while chunk := saved.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != media["sha256"]:
                    raise HTTPException(
                        409, "Saved media differs from its checksum; no file was overwritten."
                    )
            destinations[media["member"]] = f"local://{media['bucket']}/{key}"
    return destinations


async def import_archive(
    db: AsyncSession, file: BinaryIO, manifest: dict[str, Any], actor_id: uuid.UUID, dry_run: bool
) -> dict[str, Any]:
    session_id = uuid.UUID(manifest["session_id"])
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(session_id.bytes[:8], "big", signed=True)},
    )
    # Canonical media destinations are deterministic, including during preview.
    media = {
        item[
            "member"
        ]: f"local://{item['bucket']}/imports/{item['sha256']}{Path(item['member']).suffix}"
        for item in manifest["media"]
    }
    additions: list[tuple[type[Base], dict[str, Any]]] = []
    conflicts: list[dict[str, str]] = []
    existing_count = 0
    media_hashes = {item["member"]: item["sha256"] for item in manifest["media"]}
    source_hashes: dict[str, str] = {}
    for model in MODELS:
        table = model.__tablename__
        primary = [attr.key for attr in inspect(model).column_attrs if attr.columns[0].primary_key]
        incoming = manifest["entities"].get(table, [])
        existing_rows = await rows_for(
            db, model, primary[0], list({uuid.UUID(raw[primary[0]]) for raw in incoming}), lock=True
        )
        by_identity = {tuple(getattr(row, key) for key in primary): row for row in existing_rows}
        for raw in incoming:
            try:
                values = imported_record(model, raw, actor_id, media)
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, f"Invalid {table} value") from exc
            identity = tuple(values[key] for key in primary)
            existing = by_identity.get(identity)
            if existing is not None:
                before = record(existing)
                # Existing source-side media can use other keys, but must match exact bytes.
                for field in MEDIA_FIELDS:
                    if raw.get(field) and before.get(field) != values.get(field):
                        digest = hashlib.sha256()
                        try:
                            if before[field] not in source_hashes:
                                with read_media(before[field]) as source:
                                    while chunk := source.read(1024 * 1024):
                                        digest.update(chunk)
                                source_hashes[before[field]] = digest.hexdigest()
                            if (
                                source_hashes[before[field]]
                                == media_hashes[raw[field]["archive_media"]]
                            ):
                                before[field] = values[field]
                        except (OSError, ValueError, KeyError):
                            pass
                if comparable(model, before) != comparable(model, values):
                    conflicts.append(
                        {
                            "table": table,
                            "id": str(identity[0]),
                            "reason": "Existing data differs; nothing will be overwritten.",
                        }
                    )
                else:
                    existing_count += 1
            else:
                additions.append((model, values))
    result = {
        "dry_run": dry_run,
        "session_id": str(session_id),
        "title": manifest["entities"]["football_sessions"][0]["title"],
        "created": len(additions),
        "skipped": existing_count,
        "conflicts": conflicts,
        "media_count": len(manifest["media"]),
    }
    if conflicts:
        if not dry_run:
            raise HTTPException(409, result)
        return result
    if not dry_run:
        from starlette.concurrency import run_in_threadpool

        await run_in_threadpool(write_import_media, file, manifest)
        # Parent tables are flushed in dependency order; no existing row is updated.
        for model in MODELS:
            for candidate, values in additions:
                if candidate is model:
                    db.add(model(**values))
            await db.flush()
    return result
