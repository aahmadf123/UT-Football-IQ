"""Round-trip tests for the jobs router (Issue #107).

Covers list/filter, create, get, status update, and retry behavior for
``/api/v1/jobs`` using the standard mocked-AsyncSession pattern shared
with ``test_videos.py`` / ``test_clips.py``.
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


def _make_job(**overrides: Any) -> ProcessingJob:
    j = MagicMock(spec=ProcessingJob)
    j.id = overrides.get("id", uuid.uuid4())
    j.video_id = overrides.get("video_id", uuid.uuid4())
    j.clip_id = overrides.get("clip_id", None)
    j.job_type = overrides.get("job_type", JobType.detect)
    j.status = overrides.get("status", JobStatus.queued)
    j.priority = overrides.get("priority", 0)
    j.pipeline_mode = overrides.get("pipeline_mode", PipelineMode.nightly)
    j.error_stage = overrides.get("error_stage", None)
    j.error_message = overrides.get("error_message", None)
    j.nightly_followup_job_id = overrides.get("nightly_followup_job_id", None)
    j.input_artifacts = overrides.get("input_artifacts", None)
    j.output_artifacts = overrides.get("output_artifacts", None)
    j.model_version_id = overrides.get("model_version_id", None)
    j.progress = overrides.get("progress", None)
    j.attempt_count = overrides.get("attempt_count", 0)
    j.leased_by = overrides.get("leased_by", None)
    j.lease_expires_at = overrides.get("lease_expires_at", None)
    j.max_attempts = overrides.get("max_attempts", 3)
    j.started_at = overrides.get("started_at", None)
    j.finished_at = overrides.get("finished_at", None)
    j.created_at = overrides.get("created_at", datetime.now(UTC))
    return j


def _make_video(**overrides: Any) -> Video:
    v = MagicMock(spec=Video)
    v.id = overrides.get("id", uuid.uuid4())
    v.filename = overrides.get("filename", "t.mp4")
    v.storage_uri = overrides.get("storage_uri", "r2://x/t.mp4")
    v.status = overrides.get("status", VideoStatus.uploaded)
    v.created_at = overrides.get("created_at", datetime.now(UTC))
    return v


def _override_auth(role: UserRole = UserRole.coach) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)


# ── List jobs (filters compile correctly) ─────────────────────────────────────


def test_list_jobs_returns_serialized_jobs() -> None:
    jobs = [_make_job(status=JobStatus.queued), _make_job(status=JobStatus.running)]

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
            resp = c.get("/api/v1/jobs")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert {row["status"] for row in body} == {"queued", "running"}


def test_list_jobs_filters_by_status_job_type_and_video() -> None:
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
            resp = c.get(
                "/api/v1/jobs",
                params={
                    "status": "failed",
                    "job_type": "detect",
                    "video_id": str(video_id),
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    sql = seen_sql["sql"]
    assert "WHERE" in sql
    assert "processing_jobs.status" in sql.split("WHERE")[1]
    assert "processing_jobs.job_type" in sql.split("WHERE")[1]
    assert "processing_jobs.video_id" in sql.split("WHERE")[1]


def test_list_jobs_rejects_invalid_status() -> None:
    async def _db() -> AsyncGenerator[Any, None]:
        yield AsyncMock()

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/jobs", params={"status": "exploded"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422


# ── Get job detail ────────────────────────────────────────────────────────────


def test_get_job_returns_full_detail() -> None:
    job = _make_job(
        status=JobStatus.failed,
        error_stage="detect",
        error_message="cuda OOM",
        input_artifacts={"src": "r2://x/in.mp4"},
        output_artifacts=None,
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
            resp = c.get(f"/api/v1/jobs/{job.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(job.id)
    assert body["status"] == "failed"
    assert body["error_stage"] == "detect"
    assert body["error_message"] == "cuda OOM"
    assert body["input_artifacts"] == {"src": "r2://x/in.mp4"}


def test_get_job_returns_404_when_missing() -> None:
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
            resp = c.get(f"/api/v1/jobs/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── Create job round-trip (input_artifacts persistence) ───────────────────────


def test_create_job_persists_input_artifacts_and_pipeline_mode() -> None:
    video = _make_video()
    captured: list[ProcessingJob] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            result.scalar_one_or_none.return_value = video
            return result

        async def _flush() -> None:
            for j in captured:
                if getattr(j, "created_at", None) is None:
                    j.created_at = datetime.now(UTC)

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/jobs",
                json={
                    "video_id": str(video.id),
                    "job_type": "detect",
                    "priority": 10,  # → same_session mode
                    "input_artifacts": {"src": "r2://x/in.mp4"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["video_id"] == str(video.id)
    assert body["job_type"] == "detect"
    assert body["status"] == "queued"
    assert body["priority"] == 10
    assert body["is_same_session"] is True
    assert body["pipeline_mode"] == PipelineMode.same_session.value
    assert body["input_artifacts"] == {"src": "r2://x/in.mp4"}

    assert len(captured) == 1
    persisted = captured[0]
    assert persisted.video_id == video.id
    assert persisted.job_type == JobType.detect
    assert persisted.status == JobStatus.queued
    assert persisted.priority == 10
    assert persisted.pipeline_mode == PipelineMode.same_session
    assert persisted.input_artifacts == {"src": "r2://x/in.mp4"}


def test_create_job_returns_404_when_video_missing() -> None:
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
                "/api/v1/jobs",
                json={"video_id": str(uuid.uuid4()), "job_type": "detect"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


# ── Status update sets started_at / finished_at appropriately ─────────────────


def test_update_job_status_to_running_stamps_started_at() -> None:
    job = _make_job(status=JobStatus.queued, started_at=None, finished_at=None)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        session.execute = AsyncMock(return_value=result)
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.patch(f"/api/v1/jobs/{job.id}", json={"status": "running"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "running"
    assert job.started_at is not None
    assert job.finished_at is None


def test_update_job_status_to_failed_stamps_finished_at_and_error() -> None:
    job = _make_job(status=JobStatus.running, started_at=datetime.now(UTC))

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        session.execute = AsyncMock(return_value=result)
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.patch(
                f"/api/v1/jobs/{job.id}",
                json={
                    "status": "failed",
                    "error_stage": "track",
                    "error_message": "nan in homography",
                    "output_artifacts": {"log": "r2://x/log.txt"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error_stage"] == "track"
    assert body["error_message"] == "nan in homography"
    assert body["output_artifacts"] == {"log": "r2://x/log.txt"}
    assert job.finished_at is not None


def _patch_client(job: ProcessingJob, role: UserRole = UserRole.coach) -> TestClient:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        session.execute = AsyncMock(return_value=result)
        session.flush = AsyncMock()
        yield session

    _override_auth(role)
    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_update_leased_running_job_requires_lease_holder() -> None:
    job = _make_job(status=JobStatus.running, leased_by="worker-a")
    try:
        with _patch_client(job) as c:
            hijack = c.patch(f"/api/v1/jobs/{job.id}", json={"status": "succeeded"})
            wrong = c.patch(
                f"/api/v1/jobs/{job.id}", json={"status": "succeeded", "worker_id": "worker-b"}
            )
            holder = c.patch(
                f"/api/v1/jobs/{job.id}", json={"status": "succeeded", "worker_id": "worker-a"}
            )
    finally:
        app.dependency_overrides.clear()

    assert hijack.status_code == 409
    assert wrong.status_code == 409
    assert holder.status_code == 200, holder.text
    assert holder.json()["status"] == "succeeded"


def test_admin_can_override_lease_for_ops_cancel() -> None:
    job = _make_job(status=JobStatus.running, leased_by="worker-a")
    try:
        with _patch_client(job, role=UserRole.admin) as c:
            resp = c.patch(f"/api/v1/jobs/{job.id}", json={"status": "cancelled"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_update_rejects_oversized_artifact_payloads() -> None:
    job = _make_job(status=JobStatus.queued)
    try:
        with _patch_client(job) as c:
            resp = c.patch(
                f"/api/v1/jobs/{job.id}",
                json={"status": "succeeded", "output_artifacts": {"blob": "x" * 70_000}},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 413


# ── Retry job ─────────────────────────────────────────────────────────────────


def test_retry_failed_job_creates_new_queued_copy() -> None:
    original = _make_job(
        status=JobStatus.failed,
        job_type=JobType.detect,
        priority=5,
        pipeline_mode=PipelineMode.nightly,
        input_artifacts={"src": "r2://x/in.mp4"},
    )
    captured: list[ProcessingJob] = []

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = original

        async def _flush() -> None:
            for j in captured:
                if getattr(j, "created_at", None) is None:
                    j.created_at = datetime.now(UTC)

        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/v1/jobs/{original.id}/retry")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_type"] == "detect"
    assert body["priority"] == 5
    assert body["input_artifacts"] == {"src": "r2://x/in.mp4"}
    assert body["id"] != str(original.id)

    assert len(captured) == 1
    new_job = captured[0]
    assert new_job.video_id == original.video_id
    assert new_job.job_type == original.job_type
    assert new_job.status == JobStatus.queued


def test_retry_job_rejects_non_failed_status() -> None:
    job = _make_job(status=JobStatus.succeeded)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        session.execute = AsyncMock(return_value=result)
        session.add = MagicMock()
        session.flush = AsyncMock()
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/v1/jobs/{job.id}/retry")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409
    assert "failed" in resp.text.lower()
