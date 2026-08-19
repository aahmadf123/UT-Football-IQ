"""Logical→physical bucket mapping at the boto3 boundary.

Mirrors ``backend/app/storage.py``: the database stores *logical* URIs
(``s3://raw-video/…``) while a deployment provisions prefixed buckets
(``footiq-raw-video`` on R2). Without the mapping, every worker download in a
cloud deployment fails with NoSuchBucket — the worker literally cannot process
film against the deployed stack.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline import storage

_BUCKET_VARS = (
    "S3_BUCKET_RAW",
    "S3_BUCKET_CLIPS",
    "S3_BUCKET_OVERLAYS",
    "S3_BUCKET_ARTIFACTS",
)


@pytest.fixture(autouse=True)
def _clear_bucket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _BUCKET_VARS:
        monkeypatch.delenv(var, raising=False)


def test_physical_bucket_passes_through_without_env() -> None:
    assert storage.physical_bucket("raw-video") == "raw-video"
    assert storage.physical_bucket("some-legacy-bucket") == "some-legacy-bucket"


def test_physical_bucket_maps_all_logical_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET_RAW", "footiq-raw-video")
    monkeypatch.setenv("S3_BUCKET_CLIPS", "footiq-clips")
    monkeypatch.setenv("S3_BUCKET_OVERLAYS", "footiq-overlays")
    monkeypatch.setenv("S3_BUCKET_ARTIFACTS", "footiq-artifacts")
    assert storage.physical_bucket("raw-video") == "footiq-raw-video"
    assert storage.physical_bucket("clips") == "footiq-clips"
    assert storage.physical_bucket("overlays") == "footiq-overlays"
    assert storage.physical_bucket("artifacts") == "footiq-artifacts"
    # Unknown names still pass through so pre-mapping buckets stay reachable.
    assert storage.physical_bucket("some-legacy-bucket") == "some-legacy-bucket"


def test_download_hits_the_physical_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET_RAW", "footiq-raw-video")
    calls: dict[str, str] = {}

    class FakeClient:
        def download_fileobj(self, bucket: str, key: str, fileobj: Any) -> None:
            calls["bucket"] = bucket
            calls["key"] = key
            fileobj.write(b"video-bytes")

    monkeypatch.setattr(storage, "_s3_client", lambda: FakeClient())
    path = storage.download_to_temp("s3://raw-video/raw/practice.mp4")
    try:
        assert path.read_bytes() == b"video-bytes"
    finally:
        path.unlink()
    assert calls["bucket"] == "footiq-raw-video"
    # The key is untouched — only the bucket name is deployment-specific.
    assert calls["key"] == "raw/practice.mp4"


def test_upload_hits_physical_bucket_but_returns_logical_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("S3_BUCKET_OVERLAYS", "footiq-overlays")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    calls: dict[str, str] = {}

    class FakeClient:
        def upload_fileobj(self, fh: Any, bucket: str, key: str, ExtraArgs: Any = None) -> None:  # noqa: N803
            calls["bucket"] = bucket
            calls["key"] = key

    monkeypatch.setattr(storage, "_s3_client", lambda: FakeClient())
    src = tmp_path / "overlay.m3u8"
    src.write_bytes(b"#EXTM3U")
    uri = storage.upload_file(src, "overlays/clip-1/index.m3u8")

    assert calls["bucket"] == "footiq-overlays"
    # Stored URIs stay logical so they keep resolving if buckets are renamed.
    assert uri == "s3://overlays/overlays/clip-1/index.m3u8"


def test_upload_bytes_maps_bucket_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET_ARTIFACTS", "footiq-artifacts")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    calls: dict[str, str] = {}

    class FakeClient:
        def upload_fileobj(self, fh: Any, bucket: str, key: str, ExtraArgs: Any = None) -> None:  # noqa: N803
            calls["bucket"] = bucket

    monkeypatch.setattr(storage, "_s3_client", lambda: FakeClient())
    uri = storage.upload_bytes(b"{}", "tracks/clip-1.json")

    assert calls["bucket"] == "footiq-artifacts"
    assert uri == "s3://artifacts/tracks/clip-1.json"
