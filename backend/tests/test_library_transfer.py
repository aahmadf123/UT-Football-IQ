"""Session package integrity and real PostgreSQL import/replay acceptance."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    Clip,
    CoachCorrection,
    CorrectionType,
    FootballSession,
    Label,
    Play,
    Player,
    PlayView,
    Tracklet,
    TrackPoint,
    User,
    UserRole,
    Video,
)
from app.services.library_transfer import (
    build_archive,
    import_archive,
    session_graph,
    validate_archive,
)
from app.storage import ARTIFACTS, local_object_path
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def archive_library(monkeypatch, tmp_path) -> AsyncGenerator[tuple, None]:
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(get_settings().database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    actor_id, session_id, video_id, clip_id, player_id = [uuid.uuid4() for _ in range(5)]
    label_id = uuid.uuid4()
    local_object_path("raw-video", "original.mp4").parent.mkdir(parents=True)
    local_object_path("raw-video", "original.mp4").write_bytes(b"session media checksum fixture")
    async with maker() as db:
        db.add(
            User(
                id=actor_id,
                email=f"archive-{actor_id}@test.local",
                hashed_password="not-exported",
                full_name="Archive coach",
                role=UserRole.admin,
            )
        )
        await db.flush()
        db.add(Player(id=player_id, first_name="Test", last_name="Athlete", position_group="Skill"))
        db.add(FootballSession(id=session_id, title="Portable practice", created_by=actor_id))
        await db.flush()
        db.add(
            Video(
                id=video_id,
                filename="practice.mp4",
                storage_uri="local://raw-video/original.mp4",
                practice_session_id=session_id,
                uploaded_by=actor_id,
                metadata_={
                    "local_path": "/private/source.mp4",
                    "secret_token": "excluded-test-value",
                    "tags": ["red zone"],
                },
            )
        )
        await db.flush()
        db.add(
            Clip(
                id=clip_id,
                video_id=video_id,
                start_time=1,
                end_time=3,
                label_data={"_annotation_revision": 2},
                is_reviewed=True,
                reviewed_by=actor_id,
            )
        )
        play_id, track_id = uuid.uuid4(), uuid.uuid4()
        db.add(Play(id=play_id, session_id=session_id, title="Play 1"))
        await db.flush()
        db.add(PlayView(id=uuid.uuid4(), play_id=play_id, clip_id=clip_id))
        db.add(
            Tracklet(
                id=track_id,
                clip_id=clip_id,
                player_id=player_id,
                start_frame=30,
                end_frame=90,
                provenance={"source": "synthetic_test"},
            )
        )
        await db.flush()
        db.add(
            TrackPoint(id=uuid.uuid4(), tracklet_id=track_id, frame_number=30, bbox=[1, 2, 3, 4])
        )
        db.add(
            Label(
                id=label_id,
                clip_id=clip_id,
                label_type="offensive_formation",
                label_value={"formation": "trips"},
                source="human",
                annotated_by=actor_id,
            )
        )
        db.add(
            CoachCorrection(
                id=uuid.uuid4(),
                clip_id=clip_id,
                correction_type=CorrectionType.formation_tag,
                corrected_value={"formation": "trips"},
                corrected_by=actor_id,
                notes="Coach confirmed",
            )
        )
        await db.commit()
    try:
        yield maker, actor_id, session_id, clip_id, player_id
    finally:
        async with maker() as db:
            await db.execute(delete(Label).where(Label.id == label_id))
            await db.execute(delete(Video).where(Video.practice_session_id == session_id))
            await db.execute(delete(FootballSession).where(FootballSession.id == session_id))
            await db.execute(delete(Player).where(Player.id == player_id))
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_portable_media_annotations_replay_and_human_conflict(archive_library) -> None:
    maker, actor_id, session_id, clip_id, player_id = archive_library
    async with maker() as db:
        graph = await session_graph(db, session_id)
    result = build_archive(graph, session_id)
    path = local_object_path(ARTIFACTS, f"session-exports/{result['archive_id']}.zip")
    with path.open("rb") as source:
        manifest = validate_archive(source)
    text = json.dumps(manifest)
    assert "not-exported" not in text and "excluded-test-value" not in text
    assert "/private/source.mp4" not in text and "local://" not in text
    # Simulate moving the package to a fresh independent library by removing only this graph.
    async with maker() as db:
        await db.execute(delete(Label).where(Label.clip_id == clip_id))
        await db.execute(delete(Video).where(Video.practice_session_id == session_id))
        await db.execute(delete(FootballSession).where(FootballSession.id == session_id))
        await db.execute(delete(Player).where(Player.id == player_id))
        await db.commit()
    with path.open("rb") as source:
        async with maker() as db:
            preview = await import_archive(db, source, manifest, actor_id, True)
            assert preview["created"] > 0 and not preview["conflicts"]
            assert await db.get(Clip, clip_id) is None
        async with maker() as db:
            imported = await import_archive(db, source, manifest, actor_id, False)
            await db.commit()
            assert imported["created"] == preview["created"]
        async with maker() as db:
            clip = await db.get(Clip, clip_id)
            assert clip.is_reviewed and clip.label_data["_annotation_revision"] == 2
            replay = await import_archive(db, source, manifest, actor_id, False)
            assert replay["created"] == 0 and not replay["conflicts"]
            await db.commit()
        async with maker() as db:
            clip = await db.get(Clip, clip_id)
            clip.label_data = {"_annotation_revision": 3, "coach_note": "New local correction"}
            await db.commit()
        async with maker() as db:
            conflict = await import_archive(db, source, manifest, actor_id, True)
            assert conflict["conflicts"][0]["table"] == "clips"
            with pytest.raises(HTTPException) as exc:
                await import_archive(db, source, manifest, actor_id, False)
            assert exc.value.status_code == 409
            assert (await db.get(Clip, clip_id)).label_data["coach_note"] == "New local correction"


@pytest.mark.asyncio
async def test_corrupt_media_is_rejected_before_import(archive_library) -> None:
    maker, _, session_id, _, _ = archive_library
    async with maker() as db:
        graph = await session_graph(db, session_id)
    result = build_archive(graph, session_id)
    path = local_object_path(ARTIFACTS, f"session-exports/{result['archive_id']}.zip")
    bad = io.BytesIO()
    with zipfile.ZipFile(path) as archive, zipfile.ZipFile(bad, "w") as altered:
        for name in archive.namelist():
            altered.writestr(name, b"changed" if name.startswith("media/") else archive.read(name))
    with pytest.raises(HTTPException) as exc:
        validate_archive(bad)
    assert exc.value.status_code == 422


def test_archive_path_traversal_and_unknown_version_are_rejected() -> None:
    for name in ("../outside.mp4", "/absolute.mp4", "media\\escape.mp4"):
        file = io.BytesIO()
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr(name, b"video")
            archive.writestr(
                "manifest.json", json.dumps({"format": "football-iq-session", "version": 2})
            )
        with pytest.raises(HTTPException) as exc:
            validate_archive(file)
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_http_export_stream_signature_preview_and_role_guard(archive_library) -> None:
    maker, actor_id, session_id, _, _ = archive_library
    async with maker() as db:
        actor = await db.get(User, actor_id)

    async def authenticated_actor():
        return actor

    async def request_db():
        async with maker() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_current_user] = authenticated_actor
    app.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.post(f"/api/v1/library/sessions/{session_id}/export")
            assert response.status_code == 200
            exported = response.json()
            archive = await client.get(exported["download_url"])
            assert archive.status_code == 200
            assert hashlib.sha256(archive.content).hexdigest() == exported["sha256"]
            invalid = exported["download_url"].split("&sig=")[0] + "&sig=tampered"
            assert (await client.get(invalid)).status_code == 403
            preview = await client.post(
                "/api/v1/library/import",
                data={"dry_run": "true"},
                files={"file": ("practice.fiq.zip", archive.content, "application/zip")},
            )
            assert preview.status_code == 200
            assert preview.json()["created"] == 0
            assert not preview.json()["conflicts"]
            actor.role = UserRole.coach
            assert (
                await client.post(f"/api/v1/library/sessions/{session_id}/export")
            ).status_code == 403
    finally:
        app.dependency_overrides.clear()
