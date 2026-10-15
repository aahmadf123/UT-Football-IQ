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
