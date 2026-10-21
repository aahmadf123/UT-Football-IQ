"""Pytest configuration and shared fixtures for the backend test suite."""

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest
from app.config import get_settings
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def worker_python() -> str:
    """An independent worker runtime; backend-only CI has no CV dependencies.

    Full cross-service CI owns the required transport gate. These extra local
    DB-to-worker contracts run when a worker environment is installed, and may
    be explicitly selected with FOOTIQ_TEST_WORKER_PYTHON.
    """
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("FOOTIQ_TEST_WORKER_PYTHON")
    candidates = (
        ([configured] if configured else [])
        + [
            str(root / "gpu-worker" / env / executable)
            for env in (".venv-gpu", ".venv")
            for executable in ("Scripts/python.exe", "bin/python")
        ]
        + [sys.executable]
    )
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        probe = subprocess.run(  # noqa: S603 -- local test interpreter; fixed dependency probe
            [candidate, "-c", "import numpy,cv2"],
            capture_output=True,
            timeout=30,
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("Independent worker runtime unavailable; run the cross-service integration gate")


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Iterator[None]:
    """Let ``monkeypatch.setenv`` actually reach ``Settings``.

    ``get_settings`` is ``lru_cache``d, so the first call in the session pins
    every value for the rest of it. A test that sets an env var would otherwise
    silently have no effect -- and, worse, a test that *did* manage to change a
    setting would leak it into every test that ran afterwards.

    Clearing on both sides makes each test start from the real environment and
    hand back a clean cache. Note this does not affect module-level snapshots
    such as ``app.main.settings``, which are read once at import.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_no_db() -> TestClient:
    """Client with the DB dependency overridden to always raise on execute.

    This simulates an unreachable database so that /ready returns 503.
    """
    from app.database import get_db

    class _FailingSession:
        """Minimal async-session stand-in that raises on every DB call."""

        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("DB unavailable")

        async def commit(self) -> None:  # pragma: no cover
            pass

        async def rollback(self) -> None:  # pragma: no cover
            pass

        async def close(self) -> None:
            pass

    async def _unavailable_db() -> AsyncGenerator[_FailingSession, None]:
        yield _FailingSession()

    app.dependency_overrides[get_db] = _unavailable_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
