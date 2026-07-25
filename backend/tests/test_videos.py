"""Tests for the videos router (Issue #97)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    SessionKind,
    SideOfBall,
    SourceType,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from fastapi.testclient import TestClient


def _make_user() -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = UserRole.coach
    u.is_active = True
    return u


def _captured_videos() -> list[Video]:
    """A list the mocked session will append every ``add()`` into."""
    return []


def _mock_db_for_create(captured: list[Video]):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        def _add(obj: Any) -> None:
            captured.append(obj)

        async def _flush() -> None:
            # Stand in for the Postgres server_default=func.now() on created_at.
            for v in captured:
                if getattr(v, "created_at", None) is None:
                    v.created_at = datetime.now(UTC)

        session.add = MagicMock(side_effect=_add)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    return _db


def _mock_db_for_list(rows: list[Video]):
    """Returns a session whose execute() returns ``rows`` as scalars."""

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        yield session

    return _db


def _make_video(**overrides: Any) -> Video:
    """Build a fully-populated Video ORM-shaped object for response tests."""
    v = MagicMock(spec=Video)
    v.id = overrides.get("id", uuid.uuid4())
    v.filename = overrides.get("filename", "t.mp4")
    v.storage_uri = overrides.get("storage_uri", "s3://x/t.mp4")
    v.status = overrides.get("status", VideoStatus.uploaded)
    v.duration_seconds = overrides.get("duration_seconds", None)
    v.fps = overrides.get("fps", None)
    v.width = overrides.get("width", None)
    v.height = overrides.get("height", None)
    v.codec = overrides.get("codec", None)
    v.uploaded_by = overrides.get("uploaded_by", uuid.uuid4())
    v.recorded_at = overrides.get("recorded_at", None)
    v.session_kind = overrides.get("session_kind", None)
    v.source_type = overrides.get("source_type", None)
    v.opponent_team = overrides.get("opponent_team", None)
    v.practice_session_id = overrides.get("practice_session_id", None)
    v.our_possession = overrides.get("our_possession", None)
    v.capture_regime = overrides.get("capture_regime", None)
    v.regime_confidence = overrides.get("regime_confidence", None)
    v.metadata_ = overrides.get("metadata_", None)
    v.created_at = overrides.get("created_at", datetime.now(UTC))
    return v


def _override_auth(captured: list[Video]) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_for_create(captured)


def _override_auth_for_list(rows: list[Video]) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_for_list(rows)


# ── recorded_at persistence (regression test for the existing bug) ────────────


def test_create_video_persists_recorded_at() -> None:
    captured: list[Video] = []
    _override_auth(captured)
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/videos",
                json={
                    "filename": "t.mp4",
                    "storage_uri": "s3://x/t.mp4",
                    "recorded_at": "2026-05-26T14:00:00+00:00",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Pydantic emits ISO 8601 — accept either offset spelling.
    assert body["recorded_at"] in (
        "2026-05-26T14:00:00Z",
        "2026-05-26T14:00:00+00:00",
    )
    assert len(captured) == 1
    persisted = captured[0]
    assert persisted.recorded_at == datetime(2026, 5, 26, 14, 0, 0, tzinfo=UTC)


# ── session metadata round-trip ───────────────────────────────────────────────


def test_create_video_persists_session_metadata() -> None:
    captured: list[Video] = []
    _override_auth(captured)
    practice_session_id = uuid.uuid4()
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/videos",
                json={
                    "filename": "practice.mp4",
                    "storage_uri": "s3://x/practice.mp4",
                    "recorded_at": "2026-05-26T14:00:00+00:00",
                    "session_kind": "practice",
                    "source_type": "drone",
                    "practice_session_id": str(practice_session_id),
                    "our_possession": "offense",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["session_kind"] == "practice"
    assert body["source_type"] == "drone"
    assert body["practice_session_id"] == str(practice_session_id)
    assert body["our_possession"] == "offense"

    persisted = captured[0]
    assert persisted.session_kind == SessionKind.practice
    assert persisted.source_type == SourceType.drone
    assert persisted.practice_session_id == practice_session_id
    assert persisted.our_possession == SideOfBall.offense


# ── game-session validation ──────────────────────────────────────────────────


def test_create_video_game_requires_opponent() -> None:
    captured: list[Video] = []
    _override_auth(captured)
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/videos",
                json={
                    "filename": "game.mp4",
                    "storage_uri": "s3://x/game.mp4",
                    "session_kind": "game",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert "opponent_team" in resp.text
    assert captured == []  # no Video added


def test_create_video_game_with_opponent_ok() -> None:
    captured: list[Video] = []
    _override_auth(captured)
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/videos",
                json={
                    "filename": "game.mp4",
                    "storage_uri": "s3://x/game.mp4",
                    "session_kind": "game",
                    "opponent_team": "Ohio",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    assert resp.json()["opponent_team"] == "Ohio"
    assert captured[0].opponent_team == "Ohio"


# ── List filters ──────────────────────────────────────────────────────────────


def _captured_query(session_mock: AsyncMock) -> str:
    """Render the SQL of the last executed Select for substring assertions."""
    args = session_mock.execute.await_args.args
    return str(args[0])


def test_list_videos_filters_by_date_range() -> None:
    seen_query: dict[str, str] = {}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            seen_query["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/videos",
                params={
                    "recorded_after": "2026-05-20T00:00:00+00:00",
                    "recorded_before": "2026-05-27T00:00:00+00:00",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    sql = seen_query["sql"]
    assert "videos.recorded_at >=" in sql
    assert "videos.recorded_at <=" in sql


def test_list_videos_filters_by_session_kind() -> None:
    seen_query: dict[str, str] = {}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            seen_query["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/videos", params={"session_kind": "game"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert "videos.session_kind" in seen_query["sql"]


def test_list_videos_filters_by_opponent_and_practice_session() -> None:
    seen_query: dict[str, str] = {}
    psid = uuid.uuid4()

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            seen_query["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/videos",
                params={"opponent_team": "Ohio", "practice_session_id": str(psid)},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    sql = seen_query["sql"]
    assert "videos.opponent_team" in sql
    assert "videos.practice_session_id" in sql


def test_list_videos_rejects_invalid_session_kind() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_for_list([])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/videos", params={"session_kind": "tournament"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
