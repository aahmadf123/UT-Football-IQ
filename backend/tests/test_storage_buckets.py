"""Logical vs physical bucket names.

The distinction is load-bearing and was previously not made at all:
``uploads.py`` hard-coded ``"raw-video"`` at the boto3 boundary, so a deployment
that provisions ``footiq-raw-video`` failed every browser upload with
NoSuchBucket *before* the video was registered — the film simply never arrived,
with nothing in the UI to explain why.

Logical names (``raw-video``, ``clips``, …) are part of the data model: they
appear in stored ``s3://bucket/key`` URIs, in the streaming route, and in the
edge Worker's R2 binding lookup. The physical name is deployment config and must
appear nowhere except the call that talks to the object store.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from app.config import get_settings
from app.storage import (
    ARTIFACTS,
    CLIPS,
    DOWNLOADABLE_BUCKETS,
    OVERLAYS,
    RAW_VIDEO,
    generate_download_url,
    physical_bucket,
    put_object,
)


@pytest.fixture
def r2_settings(monkeypatch: Any) -> Iterator[None]:
    """Settings shaped like the real Cloudflare deployment."""
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET_RAW", "footiq-raw-video")
    monkeypatch.setenv("S3_BUCKET_CLIPS", "footiq-clips")
    monkeypatch.setenv("S3_BUCKET_OVERLAYS", "footiq-overlays")
    monkeypatch.setenv("S3_BUCKET_ARTIFACTS", "footiq-artifacts")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("r2_settings")
def test_logical_names_map_to_the_provisioned_buckets() -> None:
    assert physical_bucket(RAW_VIDEO) == "footiq-raw-video"
    assert physical_bucket(CLIPS) == "footiq-clips"
    assert physical_bucket(OVERLAYS) == "footiq-overlays"
    assert physical_bucket(ARTIFACTS) == "footiq-artifacts"


def test_unmapped_names_pass_through() -> None:
    # So a caller can still reach a bucket that predates this mapping.
    assert physical_bucket("something-else") == "something-else"


@pytest.mark.usefixtures("r2_settings")
def test_put_object_writes_to_the_physical_bucket_but_returns_a_logical_uri() -> None:
    """The whole point of the split, in one assertion.

    boto3 must see ``footiq-raw-video`` or the write fails; the stored URI must
    say ``raw-video`` or it stops resolving the day the buckets are renamed.
    """
    client = MagicMock()
    with patch("app.storage.get_s3_client", return_value=client):
        uri = put_object(RAW_VIDEO, "raw/1-a.mp4", b"data", "video/mp4")

    assert client.put_object.call_args.kwargs["Bucket"] == "footiq-raw-video"
    assert uri == "s3://raw-video/raw/1-a.mp4"


@pytest.mark.usefixtures("r2_settings")
def test_presigned_urls_are_minted_against_the_physical_bucket(monkeypatch: Any) -> None:
    monkeypatch.setenv("SIGNED_URL_MODE", "presigned")
    get_settings.cache_clear()
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://signed"
    with patch("app.storage.get_s3_client", return_value=client):
        generate_download_url(CLIPS, "clips/x.mp4")

    assert client.generate_presigned_url.call_args.kwargs["Params"]["Bucket"] == "footiq-clips"


@pytest.mark.usefixtures("r2_settings")
def test_a_stored_uri_survives_renaming_the_buckets(monkeypatch: Any) -> None:
    """A URI written before a rename still resolves after it.

    This is why the URI carries the logical name. Had it embedded the physical
    one, moving accounts or renaming a bucket would strand every existing row.
    """
    client = MagicMock()
    with patch("app.storage.get_s3_client", return_value=client):
        uri = put_object(RAW_VIDEO, "raw/1-a.mp4", b"data", "video/mp4")

    monkeypatch.setenv("S3_BUCKET_RAW", "renamed-raw-video")
    get_settings.cache_clear()

    # Same stored URI, now resolving to the new physical bucket.
    assert uri == "s3://raw-video/raw/1-a.mp4"
    assert physical_bucket(RAW_VIDEO) == "renamed-raw-video"


def test_downloadable_buckets_are_logical_names() -> None:
    """The streaming gate and the Worker must agree on the same namespace.

    ``bucketBinding()`` in workers/api-edge/src/storage.ts looks up its R2
    bindings by these exact strings; a physical name here would 404 playback.
    """
    assert DOWNLOADABLE_BUCKETS == {"raw-video", "clips", "overlays"}
    # artifacts stays out: reports are reached only through the reports route,
    # which applies ownership checks first.
    assert ARTIFACTS not in DOWNLOADABLE_BUCKETS
