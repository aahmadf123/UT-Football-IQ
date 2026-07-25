"""Local storage backend tests — put/sign/stream round trip incl. Range.

The signed /api/v1/storage route is the local-mode replacement for the
Worker's /dl/* streaming path, so it must serve Range requests (video
seeking) and refuse bad signatures, expired URLs, and traversal keys.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from app import storage
from app.config import get_settings
from fastapi.testclient import TestClient


@pytest.fixture
def local_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "public_api_base_url", "http://testserver")
    return tmp_path


def test_put_object_writes_local_uri(local_storage: Path) -> None:
    uri = storage.put_object("overlays", "clips/a/overlay.mp4", b"vid-bytes", "video/mp4")
    assert uri == "local://overlays/clips/a/overlay.mp4"
    assert (local_storage / "overlays" / "clips" / "a" / "overlay.mp4").read_bytes() == b"vid-bytes"


def test_download_url_for_uri_dispatches_on_scheme(local_storage: Path) -> None:
    storage.put_object("clips", "x/y.mp4", b"abc", "video/mp4")
    url = storage.generate_download_url_for_uri("local://clips/x/y.mp4")
    assert url.startswith("http://testserver/api/v1/storage/clips/x/y.mp4?")
    q = parse_qs(urlparse(url).query)
    assert "exp" in q and "sig" in q


def test_signature_round_trip_and_expiry(local_storage: Path) -> None:
    sig = storage.sign_stream_path("clips", "a/b.mp4", 4102444800)  # far future
    assert storage.verify_stream_signature("clips", "a/b.mp4", "4102444800", sig)
    # Wrong key or bucket fails; expired fails.
    assert not storage.verify_stream_signature("clips", "a/OTHER.mp4", "4102444800", sig)
    old_sig = storage.sign_stream_path("clips", "a/b.mp4", 1000)
    assert not storage.verify_stream_signature("clips", "a/b.mp4", "1000", old_sig)


def test_invalid_keys_rejected() -> None:
    assert not storage.is_valid_key("../etc/passwd")
    assert not storage.is_valid_key("/abs/path")
    assert not storage.is_valid_key("a\0b")
    assert storage.is_valid_key("raw/167-file.mp4")


def _signed_path(bucket: str, key: str) -> str:
    url = storage.generate_download_url(bucket, key)
    parsed = urlparse(url)
    return f"{parsed.path}?{parsed.query}"


def test_stream_route_serves_full_and_range(local_storage: Path, client: TestClient) -> None:
    payload = b"0123456789abcdef"
    storage.put_object("raw-video", "raw/clip.mp4", payload, "video/mp4")
    path = _signed_path("raw-video", "raw/clip.mp4")

    full = client.get(path)
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get(path, headers={"Range": "bytes=4-7"})
    assert partial.status_code == 206
    assert partial.content == b"4567"
    assert partial.headers["content-range"] == f"bytes 4-7/{len(payload)}"


def test_stream_route_refuses_bad_signature(local_storage: Path, client: TestClient) -> None:
    storage.put_object("raw-video", "raw/x.mp4", b"data", "video/mp4")
    path = _signed_path("raw-video", "raw/x.mp4")
    tampered = path.replace("raw/x.mp4", "raw/x.mp4")[:-4] + "beef"
    resp = client.get(tampered)
    assert resp.status_code == 403


def test_stream_route_refuses_unknown_bucket(local_storage: Path, client: TestClient) -> None:
    resp = client.get("/api/v1/storage/secrets/key?exp=1&sig=x")
    assert resp.status_code == 403


def test_stream_route_404_for_missing_object(local_storage: Path, client: TestClient) -> None:
    path = _signed_path("raw-video", "raw/missing.mp4")
    assert client.get(path).status_code == 404


def test_active_backend_auto_detects(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "")
    monkeypatch.setattr(settings, "r2_endpoint_url", "")
    assert storage.active_storage_backend() == "local"
    monkeypatch.setattr(settings, "r2_endpoint_url", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setattr(settings, "r2_access_key_id", "k")
    monkeypatch.setattr(settings, "r2_secret_access_key", "s")
    assert storage.active_storage_backend() == "r2"
