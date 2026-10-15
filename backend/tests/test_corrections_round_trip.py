"""Round-trip tests for the corrections routers (Issue #107).

Covers ``/api/v1/corrections`` (create + list), ``/api/v1/corrections/sync``
(iPad batch upload with dedup), and ``/api/v1/correction-analytics/summary``.
Uses the standard mocked-AsyncSession pattern shared with other backend tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Clip, CoachCorrection, CorrectionType, User, UserRole
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
    c.model_version_id = overrides.get("model_version_id", None)
    return c


def _make_correction(**overrides: Any) -> CoachCorrection:
    c = MagicMock(spec=CoachCorrection)
    c.id = overrides.get("id", uuid.uuid4())
    c.clip_id = overrides.get("clip_id", uuid.uuid4())
    c.correction_type = overrides.get("correction_type", CorrectionType.formation_tag)
    c.original_value = overrides.get("original_value", None)
    c.corrected_value = overrides.get("corrected_value", {"formation": "trips"})
    c.corrected_by = overrides.get("corrected_by", uuid.uuid4())
    c.notes = overrides.get("notes", None)
    c.training_eligible = overrides.get("training_eligible", True)
    c.exported_as_label = overrides.get("exported_as_label", False)
    c.created_at = overrides.get("created_at", datetime.now(UTC))
    return c


def _override_auth(role: UserRole = UserRole.coach) -> User:
    user = _make_user(role)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


# ── Create correction round-trip ──────────────────────────────────────────────


def test_create_correction_persists_all_fields() -> None:
    clip = _make_clip()
    captured: list[CoachCorrection] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            result.scalar_one_or_none.return_value = clip
            return result

        async def _flush() -> None:
            for c in captured:
                if getattr(c, "created_at", None) is None:
                    c.created_at = datetime.now(UTC)
                if getattr(c, "exported_as_label", None) is None:
                    c.exported_as_label = False

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    user = _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/corrections",
                json={
                    "clip_id": str(clip.id),
                    "correction_type": "formation_tag",
                    "original_value": {"formation": "shotgun"},
                    "corrected_value": {"formation": "trips"},
                    "notes": "QB was in pistol",
                    "training_eligible": True,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["clip_id"] == str(clip.id)
    assert body["correction_type"] == "formation_tag"
    assert body["original_value"]["client_original"] == {"formation": "shotgun"}
    assert body["corrected_value"] == {"formation": "trips"}
    assert body["notes"] == "QB was in pistol"
    assert body["training_eligible"] is True
    assert body["exported_as_label"] is False
    assert body["corrected_by"] == str(user.id)

    assert len(captured) == 1
    persisted = captured[0]
    assert persisted.clip_id == clip.id
    assert persisted.correction_type == CorrectionType.formation_tag
    assert persisted.original_value["client_original"] == {"formation": "shotgun"}
    assert persisted.corrected_value["formation"] == "trips"
    assert persisted.corrected_value["_revision"] == 1
    assert persisted.notes == "QB was in pistol"
    assert persisted.training_eligible is True
    assert persisted.corrected_by == user.id


def test_create_correction_returns_404_when_clip_missing() -> None:
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
                "/api/v1/corrections",
                json={
                    "clip_id": str(uuid.uuid4()),
                    "correction_type": "formation_tag",
                    "corrected_value": {"formation": "trips"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_create_correction_rejects_invalid_type() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        yield AsyncMock()

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/corrections",
                json={
                    "clip_id": str(uuid.uuid4()),
                    "correction_type": "not_a_real_type",
                    "corrected_value": {"x": 1},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422


# ── List corrections + filters ────────────────────────────────────────────────


def test_list_corrections_returns_rows() -> None:
    rows = [_make_correction(), _make_correction(correction_type=CorrectionType.route_tag)]

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/corrections")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert {row["correction_type"] for row in body} == {"formation_tag", "route_tag"}


def test_list_corrections_filters_by_clip_id_and_exported() -> None:
    seen_sql: dict[str, str] = {}
    clip_id = uuid.uuid4()

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            seen_sql["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/corrections",
                params={"clip_id": str(clip_id), "exported": "true"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    sql = seen_sql["sql"]
    assert "WHERE" in sql
    where_clause = sql.split("WHERE", 1)[1]
    assert "coach_corrections.clip_id" in where_clause
    assert "coach_corrections.exported_as_label" in where_clause


# ── Bulk sync (iPad) ──────────────────────────────────────────────────────────


def test_sync_corrections_persists_each_item() -> None:
    clip_a = _make_clip()
    clip_b = _make_clip()
    captured: list[CoachCorrection] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        clip_map = {clip_a.id: clip_a, clip_b.id: clip_b}

        async def _execute(stmt: Any) -> Any:
            text = str(stmt)
            result = MagicMock()
            if "FROM clips" in text:
                # Extract the clip_id from the compiled bind parameters.
                compiled = stmt.compile(compile_kwargs={"literal_binds": False})
                for val in compiled.params.values():
                    if isinstance(val, uuid.UUID) and val in clip_map:
                        result.scalar_one_or_none.return_value = clip_map[val]
                        return result
                result.scalar_one_or_none.return_value = clip_a
            else:
                # Dedup lookup → no prior correction
                result.scalar_one_or_none.return_value = None
            return result

        async def _flush() -> None:
            for c in captured:
                if getattr(c, "created_at", None) is None:
                    c.created_at = datetime.now(UTC)

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    user = _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/corrections/sync",
                json={
                    "device_id": "ipad-1",
                    "session_id": "practice-2026-05-26",
                    "corrections": [
                        {
                            "clip_id": str(clip_a.id),
                            "correction_type": "formation_tag",
                            "corrected_value": {"formation": "trips"},
                        },
                        {
                            "clip_id": str(clip_b.id),
                            "correction_type": "route_tag",
                            "corrected_value": {"route": "post"},
                            "notes": "WR ran post not slant",
                        },
                    ],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["synced_count"] == 2
    assert body["skipped_count"] == 0
    assert len(body["correction_ids"]) == 2

    assert len(captured) == 2
    assert all(c.corrected_by == user.id for c in captured)
    assert captured[0].correction_type == CorrectionType.formation_tag
    assert captured[1].correction_type == CorrectionType.route_tag
    assert captured[1].notes == "WR ran post not slant"


def test_sync_corrections_rejects_unknown_clip_atomically() -> None:
    captured: list[CoachCorrection] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            result = MagicMock()
            # Always return None → clip lookup fails for every item.
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/corrections/sync",
                json={
                    "corrections": [
                        {
                            "clip_id": str(uuid.uuid4()),
                            "correction_type": "formation_tag",
                            "corrected_value": {"formation": "trips"},
                        }
                    ],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404, resp.text
    assert captured == []


def test_sync_corrections_replays_stable_operation_id() -> None:
    clip = _make_clip()
    captured: list[CoachCorrection] = []
    existing = _make_correction(clip_id=clip.id)
    operation_id = uuid.uuid4()

    call_count = {"n": 0}
    dedup_sql: dict[str, str] = {}

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any, *_args: Any) -> Any:
            call_count["n"] += 1
            result = MagicMock()
            # 1) operation-scoped advisory lock, 2) durable operation lookup.
            if call_count["n"] % 2 == 1:
                result.scalar_one_or_none.return_value = clip
            else:
                dedup_sql["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
                result.scalar_one_or_none.return_value = existing
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/corrections/sync",
                json={
                    "corrections": [
                        {
                            "clip_id": str(clip.id),
                            "correction_type": "formation_tag",
                            "corrected_value": {"formation": "trips"},
                            "client_operation_id": str(operation_id),
                        }
                    ],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["synced_count"] == 0
    assert body["skipped_count"] == 1
    assert captured == []

    # Replay is identified by the operation, independent of elapsed time.
    assert "sql" in dedup_sql, "dedup lookup query was not captured"
    sql = dedup_sql["sql"]
    assert "WHERE" in sql
    where_clause = sql.split("WHERE", 1)[1]
    assert "coach_corrections.corrected_value" in where_clause
    assert "created_at" not in where_clause


# ── Correction analytics summary ──────────────────────────────────────────────


def test_correction_analytics_summary_handles_empty_db() -> None:
    """No corrections in DB → summary returns zero counts and stable trend."""

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            text = str(stmt).lower()
            result = MagicMock()
            if "count(" in text and "group by" not in text:
                # total count
                result.scalar.return_value = 0
            elif "group by" in text:
                # by_type aggregation
                result.all.return_value = []
            else:
                # recent corrections / weekly buckets
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/correction-analytics/summary", params={"weeks": 2})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_corrections"] == 0
    assert body["by_type"] == []
    assert body["most_corrected_labels"] == []
    assert body["rate_decline"]["decline_detected"] is False
    assert body["rate_decline"]["overall_trend"] == "stable"
    assert len(body["rate_decline"]["weeks"]) == 2


def test_correction_analytics_summary_groups_recent_corrections() -> None:
    """Recent corrections feed the most_corrected_labels counter."""
    recent = [
        _make_correction(
            correction_type=CorrectionType.formation_tag,
            original_value={"formation": "shotgun"},
        ),
        _make_correction(
            correction_type=CorrectionType.formation_tag,
            original_value={"formation": "shotgun"},
        ),
        _make_correction(
            correction_type=CorrectionType.route_tag,
            original_value={"route": "slant"},
        ),
    ]
    # Place them in the most-recent week so they show up in weekly trend too.
    now = datetime.now(UTC)
    for i, c in enumerate(recent):
        c.created_at = now - timedelta(hours=i + 1)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            text = str(stmt).lower()
            result = MagicMock()
            if "count(" in text and "group by" not in text:
                result.scalar.return_value = len(recent)
            elif "group by" in text:
                # Two type buckets: formation_tag (2), route_tag (1)
                result.all.return_value = [
                    (CorrectionType.formation_tag, 2, 2, 0),
                    (CorrectionType.route_tag, 1, 1, 0),
                ]
            else:
                # Both recent-200 query and weekly bucket queries return the
                # same list — the router filters weekly by created_at itself.
                result.scalars.return_value.all.return_value = recent
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/correction-analytics/summary", params={"weeks": 1})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_corrections"] == 3
    by_type = {row["correction_type"]: row for row in body["by_type"]}
    assert by_type["formation_tag"]["total_count"] == 2
    assert by_type["route_tag"]["total_count"] == 1

    # Most-corrected key uses "<type>:<safe-str of original_value>".
    keys = {row["label_key"]: row["count"] for row in body["most_corrected_labels"]}
    assert keys.get("formation_tag:shotgun") == 2
    assert keys.get("route_tag:slant") == 1
