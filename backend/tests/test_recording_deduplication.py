"""Shared uploads reuse real byte-identical recordings without rewriting context."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from app import storage
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import FootballSession, ProcessingJob, SystemSetting, User, UserRole, Video
from fastapi.testclient import TestClient


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "public_api_base_url", "http://testserver")
    # Probe behavior is covered separately; these bytes test content identity.
    monkeypatch.setattr("app.services.media_probe.probe_local_video", lambda *_: {})
    rows: list[Video] = []
    sessions: list[FootballSession] = []
    jobs: list[ProcessingJob] = []
    user = User(id=uuid.uuid4(), role=UserRole.coach, is_active=True)
    current = {"user": user}

    async def database():
        db = AsyncMock()

        def add(row):
            if isinstance(row, Video):
                row.created_at = datetime.now(UTC)
                rows.append(row)
            elif isinstance(row, FootballSession):
                sessions.append(row)
            elif isinstance(row, ProcessingJob):
                jobs.append(row)

        async def execute(statement):
            result = MagicMock()
            entity = statement.column_descriptions[0].get("entity")
            params = statement.compile().params
            found = None
            if entity is Video:
                found = next(
                    (
                        row
                        for row in rows
                        if row.storage_uri == params.get("storage_uri_1")
                        or row.content_sha256 == params.get("content_sha256_1")
                    ),
                    None,
                )
            elif entity is SystemSetting:
                found = SystemSetting(value={"auto_process_on_upload": False})
            result.scalar_one_or_none.return_value = found
            return result

        db.add = MagicMock(side_effect=add)
        db.execute = AsyncMock(side_effect=execute)
        yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    try:
        with TestClient(app) as client:
            yield client, rows, sessions, jobs, current
    finally:
        app.dependency_overrides.clear()


def upload(client: TestClient, filename: str, content: bytes) -> str:
    minted = client.post("/api/v1/videos/upload-url", json={"filename": filename})
    assert minted.status_code == 200
    stored = client.put(minted.json()["uploadUrl"], content=content)
    assert stored.status_code == 201
    return stored.json()["storageUri"]


def test_separate_staff_uploads_reuse_recording_and_preserve_its_context(library):
    client, rows, sessions, jobs, current = library
    content = b"actual identical stored film bytes"
    original_user = current["user"].id
    first_uri = upload(client, "team.mp4", content)
    first = client.post(
        "/api/v1/videos",
        json={"filename": "team.mp4", "storage_uri": first_uri, "tags": ["install"]},
    )
    assert first.status_code == 201, first.text
    assert first.json()["duplicate"] is False

    current["user"] = User(id=uuid.uuid4(), role=UserRole.analyst, is_active=True)
    second_uri = upload(client, "renamed.mp4", content)
    assert first_uri != second_uri
    second = client.post(
        "/api/v1/videos",
        json={
            "filename": "renamed.mp4",
            "storage_uri": second_uri,
            "practice_session_id": str(uuid.uuid4()),
            "library_role": "opponent",
            "tags": ["different tag"],
            # A client's digest is never identity or evidence of an upload.
            "content_sha256": "0" * 64,
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["filename"] == "team.mp4"
    assert second.json()["practice_session_id"] == first.json()["practice_session_id"]
    assert second.json()["metadata_"] == {"tags": ["install"]}
    assert rows[0].uploaded_by == original_user
    assert rows[0].content_sha256 == hashlib.sha256(content).hexdigest()
    assert rows[0].size_bytes == len(content)
    assert len(rows) == len(sessions) == 1
    assert jobs == []


def test_different_bytes_with_same_name_and_size_are_distinct_recordings(library):
    client, rows, sessions, _, _ = library
    for content in (b"film-A", b"film-B"):
        uri = upload(client, "practice.mp4", content)
        response = client.post(
            "/api/v1/videos", json={"filename": "practice.mp4", "storage_uri": uri}
        )
        assert response.status_code == 201, response.text
    assert len(rows) == len(sessions) == 2
    assert rows[0].content_sha256 != rows[1].content_sha256


def test_registration_refuses_unfinalized_empty_or_nonraw_objects(library):
    client, _, _, _, _ = library
    for uri, expected in (
        ("local://raw-video/raw/missing.mp4", 409),
        ("s3://clips/derived.mp4", 400),
    ):
        response = client.post("/api/v1/videos", json={"filename": "bad.mp4", "storage_uri": uri})
        assert response.status_code == expected, response.text
    storage.put_object("raw-video", "empty.mp4", b"", "video/mp4")
    response = client.post(
        "/api/v1/videos",
        json={"filename": "empty.mp4", "storage_uri": "local://raw-video/empty.mp4"},
    )
    assert response.status_code == 422, response.text


def test_registration_refuses_a_changed_previously_registered_object(library):
    client, rows, _, _, _ = library
    uri = upload(client, "original.mp4", b"original bytes")
    created = client.post("/api/v1/videos", json={"filename": "original.mp4", "storage_uri": uri})
    assert created.status_code == 201
    _, bucket, key = storage.parse_storage_uri(uri)
    # Simulate an out-of-band storage overwrite. It cannot silently change the
    # bytes associated with an existing recording's annotations and digest.
    storage.local_object_path(bucket, key).write_bytes(b"replaced bytes")
    replay = client.post("/api/v1/videos", json={"filename": "original.mp4", "storage_uri": uri})
    assert replay.status_code == 409, replay.text
    assert len(rows) == 1
    assert rows[0].content_sha256 == hashlib.sha256(b"original bytes").hexdigest()


def test_remote_fingerprint_reads_bytes_instead_of_trusting_object_metadata(monkeypatch):
    content = b"remote film" * 200_000
    source = io.BytesIO(content)
    client = MagicMock()
    client.get_object.return_value = {
        "Body": source,
        "ETag": "not-a-content-digest-3",
        "Metadata": {"sha256": "0" * 64},
    }
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    digest, size = storage.recording_fingerprint("s3://raw-video/raw/film.mp4", len(content))
    assert digest == hashlib.sha256(content).hexdigest()
    assert size == len(content)
    assert source.closed
    assert client.get_object.call_args.kwargs["Bucket"] == get_settings().s3_bucket_raw


def test_remote_fingerprint_limits_stream_size_and_closes_body(monkeypatch):
    source = io.BytesIO(b"oversized")
    client = MagicMock()
    client.get_object.return_value = {"Body": source}
    monkeypatch.setattr(storage, "get_s3_client", lambda: client)
    with pytest.raises(OverflowError):
        storage.recording_fingerprint("s3://raw-video/raw/film.mp4", 3)
    assert source.closed
