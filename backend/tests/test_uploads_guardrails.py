"""Upload endpoint guardrails (security review follow-ups).

The Worker-parity upload path is staff-only, enforces a byte ceiling both
up-front (Content-Length) and while streaming (the header can lie), and
refuses to overwrite an existing object.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from fastapi.testclient import TestClient


@pytest.fixture
def local_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "public_api_base_url", "http://testserver")
    return tmp_path


def _as_role(role: UserRole) -> None:
    def _user() -> User:
        u = MagicMock(spec=User)
        u.id = uuid.uuid4()
        u.role = role
        u.is_active = True
        return u

    app.dependency_overrides[get_current_user] = _user


@pytest.fixture
def cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def test_viewer_cannot_mint_or_put_uploads(local_storage: Path, cleanup_overrides) -> None:
    _as_role(UserRole.viewer)
    with TestClient(app) as c:
        assert c.post("/api/v1/videos/upload-url", json={"filename": "a.mp4"}).status_code == 403
        assert c.put("/api/v1/videos/upload/raw/1-a.mp4", content=b"x").status_code == 403


def test_staff_upload_round_trip_and_no_overwrite(local_storage: Path, cleanup_overrides) -> None:
    _as_role(UserRole.coach)
    with TestClient(app) as c:
        minted = c.post("/api/v1/videos/upload-url", json={"filename": "a.mp4"})
        assert minted.status_code == 200
        key = minted.json()["key"]

        first = c.put(f"/api/v1/videos/upload/{key}", content=b"movie-bytes")
        assert first.status_code == 201, first.text
        assert first.json()["size"] == len(b"movie-bytes")
        assert first.json()["storageUri"] == f"local://raw-video/{key}"

        # Same key again: refused, original bytes intact.
        again = c.put(f"/api/v1/videos/upload/{key}", content=b"clobber")
        assert again.status_code == 409
        assert (local_storage / "raw-video" / key).read_bytes() == b"movie-bytes"


def test_upload_rejects_declared_oversize(
    local_storage: Path, cleanup_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10)
    _as_role(UserRole.coach)
    with TestClient(app) as c:
        resp = c.put(
            "/api/v1/videos/upload/raw/1-big.mp4",
            content=b"x" * 11,  # httpx sets Content-Length → rejected up front
        )
    assert resp.status_code == 413


def test_upload_enforces_ceiling_mid_stream_and_leaves_no_partial(
    local_storage: Path, cleanup_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10)
    _as_role(UserRole.coach)

    def _chunks():  # generator body → chunked transfer, no Content-Length
        yield b"12345"
        yield b"6789012345"

    with TestClient(app) as c:
        resp = c.put("/api/v1/videos/upload/raw/1-chunked.mp4", content=_chunks())
    assert resp.status_code == 413
    assert not (local_storage / "raw-video" / "raw" / "1-chunked.mp4").exists()
