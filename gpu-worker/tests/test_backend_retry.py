"""One dropped connection or API restart must not fail an hours-long job."""

from __future__ import annotations

import httpx
import pytest

from pipeline import backend


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend.time, "sleep", lambda _s: None)


def _client(handler) -> backend._WorkerClient:
    return backend._WorkerClient(
        base_url="https://api.test", transport=httpx.MockTransport(handler)
    )


def test_idempotent_create_is_retried_after_a_reset_and_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inside a leased job every create gets a content-derived id.
    monkeypatch.setattr(backend, "stable_record_id", lambda _path, _payload: "stable-id")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        if len(seen) == 1:
            raise httpx.ReadError("[WinError 10054] connection forcibly closed")
        if len(seen) == 2:
            return httpx.Response(503, json={"detail": "The server is restarting."})
        return httpx.Response(201, json={"id": "t1"})

    payload = {"clip_id": "c1", "start_frame": 0, "end_frame": 9, "track_points": []}
    with _client(handler) as client:
        response = client.post("/api/v1/tracklets", json=payload)
    assert response.status_code == 201
    assert len(seen) == 3
    # Every attempt carries the same stable id, so a duplicate can't be created.
    assert len(set(seen)) == 1 and '"id"' in seen[0]


def test_non_idempotent_post_is_not_replayed_after_it_may_have_landed() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadError("connection reset")

    with _client(handler) as client, pytest.raises(httpx.ReadError):
        client.post("/api/v1/jobs/claim", json={"worker_id": "w"})
    assert calls["n"] == 1


def test_any_request_is_retried_when_the_connection_never_opened() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(204)

    with _client(handler) as client:
        assert client.post("/api/v1/jobs/claim", json={"worker_id": "w"}).status_code == 204
    assert calls["n"] == 3


def test_gives_up_after_the_last_attempt_and_returns_the_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "restarting"})

    with _client(handler) as client:
        response = client.patch("/api/v1/clips/c1", json={"label_data": {}})
    assert response.status_code == 503


def test_client_errors_are_returned_at_once() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, json={"detail": "bad"})

    with _client(handler) as client:
        assert client.get("/api/v1/clips/c1").status_code == 422
    assert calls["n"] == 1
