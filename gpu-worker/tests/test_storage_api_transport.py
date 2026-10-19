"""Remote film transport delegates access through the authenticated API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pipeline import storage


@pytest.fixture
def api_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIPELINE_STORAGE_TRANSPORT", "api")
    monkeypatch.setenv("BACKEND_API_URL", "https://team.test")
    monkeypatch.setattr("worker.auth.token", lambda: "test-worker-token")


def test_reads_remote_film_with_delegated_signed_access(
    api_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"download_url": "https://objects.test/signed-film"})
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"actual-object-bytes")

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    path = storage.download_to_temp("s3://raw-video/raw/practice.mp4")
    try:
        assert path.read_bytes() == b"actual-object-bytes"
    finally:
        path.unlink()
    assert json.loads(observed[0].content) == {"uri": "s3://raw-video/raw/practice.mp4"}
    assert observed[0].headers["Authorization"] == "Bearer test-worker-token"


def test_restart_checkpoint_lookup_uses_library_namespace(
    api_storage: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200, json={"download_url": "/api/v1/system/storage/download/artifacts/stage.json"}
            )
        return httpx.Response(200, content=b"{}")

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    path = storage.download_to_temp("api://artifacts/pipeline_artifacts/video/stage.json")
    path.unlink()
    assert bodies == [{"uri": "library://artifacts/pipeline_artifacts/video/stage.json"}]


def test_upload_publishes_to_remote_library_instead_of_local_disk(
    api_storage: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.method == "PUT"
        assert request.content == b"checkpoint-bytes"
        return httpx.Response(201, json={"uri": "local://artifacts/models/candidate.npz"})

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    candidate = tmp_path / "candidate.npz"
    candidate.write_bytes(b"checkpoint-bytes")
    assert (
        storage.upload_file(candidate, "models/candidate.npz")
        == "local://artifacts/models/candidate.npz"
    )
    assert observed[0].url.path == "/api/v1/system/storage/artifacts/models/candidate.npz"


def test_interrupted_download_does_not_leave_partial_temp_files(
    api_storage: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pipeline.runtime import PipelineInterrupted, job_context

    control = {"action": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"download_url": "https://objects.test/film"})
        control["action"] = "cancel"
        return httpx.Response(200, content=b"partial-film")

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    monkeypatch.setattr(storage.tempfile, "tempdir", str(tmp_path))
    with job_context(control=lambda: control["action"]), pytest.raises(PipelineInterrupted):
        storage.download_to_temp("s3://raw-video/film.mp4")
    assert list(tmp_path.iterdir()) == []


def test_hosted_download_stops_at_byte_cap_and_removes_partial_file(
    api_storage: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pipeline import hosted_limits

    monkeypatch.setenv("PIPELINE_HOSTED", "1")
    monkeypatch.setattr(hosted_limits, "MAX_SOURCE_BYTES", 8)
    monkeypatch.setattr(storage.tempfile, "tempdir", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"download_url": "https://objects.test/signed-film"})
        return httpx.Response(200, content=b"more-than-eight-bytes")

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    with pytest.raises(hosted_limits.HostedCapacityError, match="512 MiB"):
        storage.download_to_temp("api://raw-video/raw/other.mp4")
    assert list(tmp_path.iterdir()) == []


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(storage.time, "sleep", lambda _seconds: None)


FILM = bytes(range(256)) * 40


def test_short_stream_resumes_with_a_range_request(
    api_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ranges: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json={"download_url": "https://objects.test/film", "size_bytes": len(FILM)}
            )
        ranges.append(request.headers.get("Range"))
        if "Range" not in request.headers:
            return httpx.Response(200, content=FILM[:3000])  # ended early, no error
        start = int(request.headers["Range"].removeprefix("bytes=").rstrip("-"))
        return httpx.Response(206, content=FILM[start:])

    _mock_client(monkeypatch, handler)
    path = storage.download_to_temp("s3://raw-video/raw/game.mp4")
    try:
        assert path.read_bytes() == FILM
    finally:
        path.unlink()
    assert ranges == [None, "bytes=3000-"]


def test_size_from_the_download_header_and_a_server_error_retry(
    api_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"download_url": "https://objects.test/film"})
        calls["get"] += 1
        if calls["get"] == 1:
            return httpx.Response(503, json={"detail": "The server is restarting."})
        return httpx.Response(200, content=FILM, headers={"X-Object-Size": str(len(FILM))})

    _mock_client(monkeypatch, handler)
    path = storage.download_to_temp("s3://raw-video/raw/game.mp4")
    try:
        assert path.read_bytes() == FILM
    finally:
        path.unlink()
    assert calls["get"] == 2


def test_a_download_that_never_completes_fails_instead_of_returning_a_short_file(
    api_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200, json={"download_url": "https://objects.test/film", "size_bytes": len(FILM)}
            )
        return httpx.Response(200, content=FILM[:100])  # server ignores Range

    _mock_client(monkeypatch, handler)
    with pytest.raises(OSError, match="Download incomplete"):
        storage.download_to_temp("s3://raw-video/raw/game.mp4")


def test_access_errors_are_not_retried(api_storage: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"download_url": "https://objects.test/film"})
        calls["get"] += 1
        return httpx.Response(403, json={"detail": "Invalid or expired object access"})

    _mock_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        storage.download_to_temp("s3://raw-video/raw/game.mp4")
    assert calls["get"] == 1
