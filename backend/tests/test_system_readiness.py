"""Processing readiness must distinguish model-free review from tracking."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.routers.system import system_status


@pytest.mark.parametrize(
    "online,profiles,detector,review,quick,processing",
    [
        (True, ["review"], False, True, False, True),
        (True, ["review", "quick"], True, True, True, True),
        (False, ["review", "quick"], True, False, False, False),
        (True, ["quick"], True, False, True, True),
        (True, ["quick"], False, False, False, False),
    ],
)
async def test_profiles_require_a_live_capable_worker(
    monkeypatch,
    tmp_path,
    online,
    profiles,
    detector,
    review,
    quick,
    processing,
):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    worker = SimpleNamespace(
        worker_id="test-worker",
        device="cpu",
        capabilities={"profiles": profiles, "models": {"detect": {"ready": detector}}},
        last_seen_at=datetime.now(UTC) - timedelta(seconds=0 if online else 120),
    )
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [worker]
    db = AsyncMock()
    db.execute.return_value = rows
    result = await system_status(db, MagicMock())
    available = {profile["id"]: profile["available"] for profile in result["profiles"]}
    assert available["review"] is review
    assert available["quick"] is quick
    assert result["capabilities"]["film_review"] is True
    assert result["capabilities"]["processing"] is processing
