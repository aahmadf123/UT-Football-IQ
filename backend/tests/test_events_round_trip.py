"""Round-trip tests for the events router (Issue #107).

Covers create, list-for-clip (with type filter), get, update, and delete
behavior for game-event records, using the standard mocked-AsyncSession
pattern shared with the other backend tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Clip, CoachCorrection, Event, User, UserRole, Video
from fastapi.testclient import TestClient


def _make_user(role: UserRole = UserRole.coach) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _make_clip(**overrides: Any) -> Clip:
    c = MagicMock(spec=Clip)
    c.id = overrides.get("id", uuid.uuid4())
    c.video_id = overrides.get("video_id", uuid.uuid4())
    c.start_time = 0.0
    c.end_time = 5.0
    return c


def _make_event(**overrides: Any) -> Event:
    e = MagicMock(spec=Event)
    e.id = overrides.get("id", uuid.uuid4())
    e.clip_id = overrides.get("clip_id", uuid.uuid4())
    e.event_type = overrides.get("event_type", "snap")
    e.frame_number = overrides.get("frame_number", None)
    e.timestamp_seconds = overrides.get("timestamp_seconds", None)
    e.attributes = overrides.get("attributes", None)
    e.created_by = overrides.get("created_by", uuid.uuid4())
    e.model_version_id = overrides.get("model_version_id", None)
    e.created_at = overrides.get("created_at", datetime.now(UTC))
    return e


def _override_auth(role: UserRole = UserRole.coach) -> User:
    user = _make_user(role)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


# ── Create event round-trip ───────────────────────────────────────────────────


def test_create_event_persists_all_fields() -> None:
    clip = _make_clip()
    captured: list[Event] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            result.scalar_one_or_none.return_value = clip
            return result

        async def _flush() -> None:
            for e in captured:
                if getattr(e, "created_at", None) is None:
                    e.created_at = datetime.now(UTC)

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    user = _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/events",
                json={
                    "clip_id": str(clip.id),
                    "event_type": "snap",
                    "frame_number": 42,
                    "timestamp_seconds": 1.4,
                    "attributes": {"qb_id": "abc", "shotgun": True},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["clip_id"] == str(clip.id)
    assert body["event_type"] == "snap"
    assert body["frame_number"] == 42
    assert body["timestamp_seconds"] == 1.4
    assert body["attributes"] == {"qb_id": "abc", "shotgun": True}
    assert body["created_by"] == str(user.id)

    assert len(captured) == 1
    persisted = captured[0]
    assert persisted.clip_id == clip.id
    assert persisted.event_type == "snap"
    assert persisted.frame_number == 42
    assert persisted.attributes == {"qb_id": "abc", "shotgun": True}
    assert persisted.created_by == user.id


def test_create_event_returns_404_when_clip_missing() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/events",
                json={"clip_id": str(uuid.uuid4()), "event_type": "snap"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── List events for clip + filter ─────────────────────────────────────────────


def test_list_events_for_clip_returns_rows() -> None:
    clip = _make_clip()
    events = [
        _make_event(clip_id=clip.id, event_type="snap", frame_number=10),
        _make_event(clip_id=clip.id, event_type="throw", frame_number=40),
    ]
    call_count = {"n": 0}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                # parent-clip lookup
                result.scalar_one_or_none.return_value = clip
            else:
                result.scalars.return_value.all.return_value = events
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{clip.id}/events")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["event_type"] for row in body] == ["snap", "throw"]
    assert [row["frame_number"] for row in body] == [10, 40]


def test_list_events_filter_by_event_type_compiles_into_sql() -> None:
    clip = _make_clip()
    seen_sql: dict[str, str] = {}
    call_count = {"n": 0}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.scalar_one_or_none.return_value = clip
            else:
                seen_sql["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/clips/{clip.id}/events",
                params={"event_type": "snap"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    sql = seen_sql["sql"]
    assert "WHERE" in sql
    assert "events.event_type" in sql.split("WHERE", 1)[1]


def test_list_events_returns_404_when_clip_missing() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/clips/{uuid.uuid4()}/events")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── Get single event ──────────────────────────────────────────────────────────


def test_get_event_returns_row() -> None:
    event = _make_event(
        event_type="catch",
        frame_number=120,
        attributes={"receiver_id": "x"},
    )

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = event
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/events/{event.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(event.id)
    assert body["event_type"] == "catch"
    assert body["frame_number"] == 120
    assert body["attributes"] == {"receiver_id": "x"}


# ── Update event ──────────────────────────────────────────────────────────────


def test_update_event_mutates_only_provided_fields() -> None:
    event = _make_event(
        event_type="throw",
        frame_number=50,
        attributes={"qb_id": "a"},
    )

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = event

        async def _execute(stmt: Any) -> Any:
            result = MagicMock()
            descriptions = getattr(stmt, "column_descriptions", [])
            entity = descriptions[0].get("entity") if descriptions else None
            result.scalar_one_or_none.return_value = (
                _make_clip(id=event.clip_id) if entity is Clip else event
            )
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.patch(
                f"/api/v1/events/{event.id}",
                json={"frame_number": 55, "attributes": {"qb_id": "b"}},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # event_type unchanged
    assert body["event_type"] == "throw"
    assert body["frame_number"] == 55
    assert body["attributes"] == {"qb_id": "b", "source": "human", "timebase": "clip"}
    assert event.frame_number == 55
    assert event.attributes == {"qb_id": "b", "source": "human", "timebase": "clip"}


# ── Delete event ──────────────────────────────────────────────────────────────


def test_delete_event_returns_204() -> None:
    event = _make_event()
    clip = _make_clip(id=event.clip_id)
    clip.label_data, clip.provenance = {}, {}
    video = Video(
        id=clip.video_id,
        filename="removal-test.mp4",
        storage_uri="local://raw/test.mp4",
        content_sha256="a" * 64,
        size_bytes=10,
    )
    statements = []
    sessions = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        sessions.append(session)

        async def execute(stmt: Any) -> Any:
            statements.append(stmt)
            result = MagicMock()
            descriptions = getattr(stmt, "column_descriptions", [])
            entity = descriptions[0].get("entity") if descriptions else None
            result.scalar_one_or_none.return_value = (
                clip if entity is Clip else video if entity is Video else event
            )
            if entity is Video:
                result.scalar_one.return_value = video
            return result

        session.execute = AsyncMock(side_effect=execute)
        session.delete = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.delete(f"/api/v1/events/{event.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 204
    assert clip.label_data["_annotation_revision"] == 1
    assert clip.label_data["_observation_revision"] == 1
    assert any(
        getattr(stmt, "is_update", False) and stmt.table.name == "metrics" for stmt in statements
    )
    sessions[0].delete.assert_not_awaited()
    assert event.provenance["state"] == "superseded"
    history = sessions[0].add.call_args.args[0]
    assert isinstance(history, CoachCorrection)
    assert history.corrected_value["removed"] is True
    assert history.original_value["event"]["id"] == str(event.id)


def test_delete_event_returns_404_when_missing() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.delete(f"/api/v1/events/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
