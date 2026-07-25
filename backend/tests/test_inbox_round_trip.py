"""Round-trip tests for the Practice Inbox integration router (Issue #107).

Covers ``/api/v1/inbox/status``, ``/api/v1/inbox/status/{job_id}``, and
``/api/v1/inbox/jobs``. The ``/status`` aggregate endpoint runs one
``videos`` query followed by per-video sub-queries for jobs/clip-count/
calibrations, so the mock session dispatches on the rendered SQL.
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
from app.models import (
    FieldCalibration,
    JobStatus,
    JobType,
    PipelineMode,
    ProcessingJob,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from fastapi.testclient import TestClient


def _make_user(role: UserRole = UserRole.coach) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _make_video(**overrides: Any) -> Video:
    v = MagicMock(spec=Video)
    v.id = overrides.get("id", uuid.uuid4())
    v.filename = overrides.get("filename", "practice.mp4")
    v.storage_uri = overrides.get("storage_uri", "r2://x/practice.mp4")
    v.status = overrides.get("status", VideoStatus.processing)
    v.created_at = overrides.get("created_at", datetime.now(UTC))
    return v


def _make_job(**overrides: Any) -> ProcessingJob:
    j = MagicMock(spec=ProcessingJob)
    j.id = overrides.get("id", uuid.uuid4())
    j.video_id = overrides.get("video_id", uuid.uuid4())
    j.clip_id = overrides.get("clip_id", None)
    j.job_type = overrides.get("job_type", JobType.detect)
    j.status = overrides.get("status", JobStatus.queued)
    j.priority = overrides.get("priority", 0)
    j.pipeline_mode = overrides.get("pipeline_mode", PipelineMode.nightly)
    j.nightly_followup_job_id = overrides.get("nightly_followup_job_id", None)
    j.started_at = overrides.get("started_at", None)
    j.finished_at = overrides.get("finished_at", None)
    j.error_stage = overrides.get("error_stage", None)
    j.error_message = overrides.get("error_message", None)
    j.created_at = overrides.get("created_at", datetime.now(UTC))
    return j


def _make_calibration(analytics_safe: bool) -> FieldCalibration:
    cal = MagicMock(spec=FieldCalibration)
    cal.analytics_safe = analytics_safe
    return cal


def _override_auth(role: UserRole = UserRole.coach) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)


# ── /inbox/status (aggregated feed) ───────────────────────────────────────────


def test_inbox_status_aggregates_jobs_clips_and_calibrations() -> None:
    video = _make_video(status=VideoStatus.processing)
    jobs = [
        _make_job(
            video_id=video.id,
            status=JobStatus.succeeded,
            priority=10,  # same-session
            job_type=JobType.pose,
        ),
        _make_job(
            video_id=video.id,
            status=JobStatus.failed,
            error_stage="track",
            error_message="lost target",
        ),
        _make_job(video_id=video.id, status=JobStatus.running),
    ]
    calibrations = [
        _make_calibration(True),
        _make_calibration(True),
        _make_calibration(False),
    ]

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            text = str(stmt).lower()
            result = MagicMock()
            if "from videos" in text:
                result.scalars.return_value.all.return_value = [video]
            elif "from processing_jobs" in text:
                result.scalars.return_value.all.return_value = jobs
            elif "from clips" in text and "count(" in text and "result_state" in text:
                # preliminary-clip count (same-session clips awaiting upgrade)
                result.scalar_one.return_value = 4
            elif "from clips" in text and "count(" in text:
                # clip count for the video
                result.scalar_one.return_value = 7
            elif "from field_calibrations" in text:
                result.scalars.return_value.all.return_value = calibrations
            else:
                # Defensive fallback
                result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/inbox/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["video_id"] == str(video.id)
    assert item["filename"] == "practice.mp4"
    assert item["video_status"] == "processing"
    assert item["total_jobs"] == 3
    assert item["running_jobs"] == 1
    assert item["succeeded_jobs"] == 1
    assert item["failed_jobs"] == 1
    assert item["clip_count"] == 7
    # Preliminary clips awaiting nightly upgrade (Issue #147).
    assert item["preliminary_clip_count"] == 4
    assert item["same_session_job_count"] == 1
    assert item["pose_pipeline_active"] is True
    # 2 of 3 calibrations safe → 66.7 %.
    assert item["calibration_safe_pct"] == 66.7
    # Latest error surfaces from the failed job.
    assert item["latest_error_stage"] == "track"
    assert item["latest_error_message"] == "lost target"


def test_inbox_status_returns_empty_list_when_no_videos() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/inbox/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_inbox_status_blocks_unauthorized_roles() -> None:
    """Players cannot view processing status."""

    async def _db() -> AsyncGenerator[Any, None]:
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.player)
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/inbox/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403


# ── /inbox/status/{job_id} ────────────────────────────────────────────────────


def test_inbox_status_for_single_job_returns_lightweight_item() -> None:
    job = _make_job(
        status=JobStatus.running,
        priority=10,
        pipeline_mode=PipelineMode.same_session,
        started_at=datetime.now(UTC),
    )

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/inbox/status/{job.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["status"] == "running"
    assert body["is_same_session"] is True
    assert body["pipeline_mode"] == PipelineMode.same_session.value
    assert body["started_at"] is not None


def test_inbox_status_for_single_job_returns_404_when_missing() -> None:
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
            resp = c.get(f"/api/v1/inbox/status/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── /inbox/jobs (active jobs) ─────────────────────────────────────────────────


def test_inbox_jobs_returns_active_jobs() -> None:
    jobs = [
        _make_job(status=JobStatus.queued, priority=10),
        _make_job(status=JobStatus.running, priority=0),
    ]

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = jobs
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/inbox/jobs")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert {row["status"] for row in body} == {"queued", "running"}
    # is_same_session derives from priority >= 10
    same_session_flags = {row["is_same_session"] for row in body}
    assert same_session_flags == {True, False}


def test_inbox_jobs_same_session_only_filter_compiles_into_sql() -> None:
    seen_sql: dict[str, str] = {}

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
            resp = c.get("/api/v1/inbox/jobs", params={"same_session_only": "true"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    sql = seen_sql["sql"]
    assert "WHERE" in sql
    where_clause = sql.split("WHERE", 1)[1]
    # Active-status filter must always be applied.
    assert "processing_jobs.status" in where_clause
    # Same-session filter is priority-based.
    assert "processing_jobs.priority" in where_clause


def test_inbox_jobs_filters_by_video_id() -> None:
    seen_sql: dict[str, str] = {}
    video_id = uuid.uuid4()

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
            resp = c.get("/api/v1/inbox/jobs", params={"video_id": str(video_id)})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    sql = seen_sql["sql"]
    assert "WHERE" in sql
    assert "processing_jobs.video_id" in sql.split("WHERE", 1)[1]
