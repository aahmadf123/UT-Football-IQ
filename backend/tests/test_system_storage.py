"""Real local bytes through the worker artifact bridge and atomic film upload."""

import asyncio
import hashlib
from unittest.mock import MagicMock

import httpx
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole


async def test_worker_artifact_put_resolve_and_download(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    user = MagicMock(spec=User)
    user.role = UserRole.analyst
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            stored = await client.put(
                "/api/v1/system/storage/artifacts/checkpoints/stage.json",
                content=b'{"frame":42}',
                headers={"content-type": "application/json"},
            )
            assert stored.status_code == 201
            assert stored.json()["uri"] == "local://artifacts/checkpoints/stage.json"
            resolved = await client.post(
                "/api/v1/system/storage/resolve",
                json={"uri": "library://artifacts/checkpoints/stage.json"},
            )
            assert resolved.status_code == 200
            assert resolved.json()["size_bytes"] == len(b'{"frame":42}')
            downloaded = await client.get(resolved.json()["download_url"])
            assert downloaded.status_code == 200 and downloaded.json() == {"frame": 42}
            assert downloaded.headers["x-object-size"] == str(len(b'{"frame":42}'))
            # Artifact capabilities stay on the role-gated resolve surface.
            generic = await client.get(
                "/api/v1/videos/download-url",
                params={"bucket": "artifacts", "key": "checkpoints/stage.json"},
            )
            assert generic.status_code == 400
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_concurrent_film_uploads_publish_one_complete_object(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    user = MagicMock(spec=User)
    user.role = UserRole.coach
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            payloads = [b"camera-one" * 1000, b"camera-two" * 1000]
            responses = await asyncio.gather(
                *[
                    client.put("/api/v1/videos/upload/raw/same.mp4", content=body)
                    for body in payloads
                ]
            )
            assert sorted(r.status_code for r in responses) == [201, 409]
            index = next(i for i, response in enumerate(responses) if response.status_code == 201)
            assert (tmp_path / "raw-video" / "raw" / "same.mp4").read_bytes() == payloads[index]
            assert responses[index].json()["sha256"] == hashlib.sha256(payloads[index]).hexdigest()
            assert list((tmp_path / "raw-video" / "raw").iterdir()) == [
                tmp_path / "raw-video" / "raw" / "same.mp4"
            ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_hosted_worker_signing_streams_artifacts_through_authorized_route(monkeypatch):
    import io

    from botocore.response import StreamingBody

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SIGNED_URL_MODE", "worker")
    monkeypatch.setenv("S3_BUCKET_ARTIFACTS", "team-artifacts")
    get_settings.cache_clear()
    user = MagicMock(spec=User)
    user.role = UserRole.analyst
    app.dependency_overrides[get_current_user] = lambda: user
    cloud = MagicMock()
    payload = b'{"hosted":true}'
    cloud.get_object.return_value = {
        "Body": StreamingBody(io.BytesIO(payload), len(payload)),
        "ContentLength": len(payload),
        "ContentType": "application/json",
    }
    cloud.head_object.return_value = {"ContentLength": len(payload)}
    monkeypatch.setattr("app.routers.system.get_s3_client", lambda: cloud)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            resolved = await client.post(
                "/api/v1/system/storage/resolve",
                json={"uri": "s3://team-artifacts/reports/export.json"},
            )
            assert resolved.status_code == 200
            url = resolved.json()["download_url"]
            assert resolved.json()["size_bytes"] == len(payload)
            assert "/api/v1/system/storage/download/artifacts/" in url
            downloaded = await client.get(url)
            assert downloaded.status_code == 200
            assert downloaded.json() == {"hosted": True}
            assert downloaded.headers["x-object-size"] == str(len(payload))
        cloud.get_object.assert_called_once_with(Bucket="team-artifacts", Key="reports/export.json")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_ranged_download_reports_the_whole_object_size(monkeypatch):
    import io

    from botocore.response import StreamingBody

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("SIGNED_URL_MODE", "worker")
    get_settings.cache_clear()
    user = MagicMock(spec=User)
    user.role = UserRole.analyst
    app.dependency_overrides[get_current_user] = lambda: user
    cloud = MagicMock()
    cloud.get_object.return_value = {
        "Body": StreamingBody(io.BytesIO(b"tail"), 4),
        "ContentLength": 4,
        "ContentRange": "bytes 96-99/100",
    }
    cloud.head_object.side_effect = RuntimeError("no head permission")
    monkeypatch.setattr("app.routers.system.get_s3_client", lambda: cloud)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            resolved = await client.post(
                "/api/v1/system/storage/resolve", json={"uri": "s3://raw-video/raw/game.mp4"}
            )
            assert resolved.status_code == 200
            assert resolved.json()["size_bytes"] is None  # advisory only
            part = await client.get(resolved.json()["download_url"], headers={"Range": "bytes=96-"})
            assert part.status_code == 206 and part.content == b"tail"
            assert part.headers["x-object-size"] == "100"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
