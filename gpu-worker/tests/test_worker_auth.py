"""Worker service-account auth: caching, backoff, and identity."""

from __future__ import annotations

import pytest

from worker.auth import WorkerAuth, worker_id


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    monkeypatch.setenv("WORKER_EMAIL", "w@example.com")
    monkeypatch.setenv("WORKER_PASSWORD", "pw")


def test_unconfigured_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_API_URL", raising=False)
    monkeypatch.delenv("WORKER_EMAIL", raising=False)
    monkeypatch.delenv("WORKER_PASSWORD", raising=False)
    assert WorkerAuth().token() is None


def test_login_failure_backs_off(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _boom(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        raise ConnectionError("backend down")

    monkeypatch.setattr("worker.auth.httpx.post", _boom)
    auth = WorkerAuth()
    assert auth.token() is None
    assert auth.token() is None  # within backoff window: no second login attempt
    assert calls["n"] == 1


def test_invalidate_clears_backoff(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _boom(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        raise ConnectionError("backend down")

    monkeypatch.setattr("worker.auth.httpx.post", _boom)
    auth = WorkerAuth()
    assert auth.token() is None
    auth.invalidate()  # a 401 path means the backend answered — retry allowed
    assert auth.token() is None
    assert calls["n"] == 2


def test_worker_id_env_override_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ID", "gpu-box-7")
    assert worker_id() == "gpu-box-7"
    monkeypatch.delenv("WORKER_ID")
    assert worker_id()  # hostname-pid fallback is non-empty
