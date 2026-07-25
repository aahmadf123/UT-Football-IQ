"""DB-as-queue claim/heartbeat tests against real Postgres.

FOR UPDATE SKIP LOCKED, lease reclaim, and attempt exhaustion are Postgres
behaviours, so these tests run the actual claim SQL against the configured
test database (same pattern as test_cfbd_sync), creating only the FK-closure
tables they need and dropping what they created afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import (
    JobStatus,
    JobType,
    ProcessingJob,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# FK closure for processing_jobs (clips ↔ processing_jobs cross-reference,
# both reach model_versions / training_datasets / field_calibrations / users).
_TABLES = [
    Base.metadata.tables["users"],
    Base.metadata.tables["training_datasets"],
    Base.metadata.tables["model_versions"],
    Base.metadata.tables["videos"],
    Base.metadata.tables["field_calibrations"],
    Base.metadata.tables["clips"],
    Base.metadata.tables["processing_jobs"],
]

_ENUM_TYPES = (
    "job_type",
    "job_status",
    "user_role",
    "video_status",
    "session_kind",
    "side_of_ball",
    "source_type",
    "capture_regime",
    "model_stage",
)


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        existing: set[str] = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    created = [t for t in _TABLES if t.name not in existing]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=created))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        if created:
            async with engine.begin() as conn:
                # Raw CASCADE drops keep teardown robust even if a previous
                # aborted run left partial state behind.
                for table in reversed(created):
                    await conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
            for enum_name in _ENUM_TYPES:
                # A type still referenced elsewhere (e.g. the alembic schema
                # pre-exists locally) must not error the teardown.
                try:
                    async with engine.begin() as conn:
                        await conn.exec_driver_sql(f"DROP TYPE IF EXISTS {enum_name}")
                except Exception:
                    pass
        await engine.dispose()


async def _seed_video(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"claim-{uuid.uuid4().hex[:8]}@test.local",
            hashed_password="x",
            full_name="Claim Tester",
            role=UserRole.analyst,
        )
        session.add(user)
        video = Video(
            id=uuid.uuid4(),
            filename="claim.mp4",
            storage_uri="local://raw-video/raw/claim.mp4",
            status=VideoStatus.uploaded,
            uploaded_by=user.id,
        )
        session.add(video)
        await session.commit()
        return video.id


async def _seed_job(
    maker: async_sessionmaker[AsyncSession],
    video_id: uuid.UUID,
    *,
    priority: int = 0,
    status: JobStatus = JobStatus.queued,
    attempt_count: int = 0,
    max_attempts: int = 3,
    lease_expires_at: datetime | None = None,
    created_offset_s: int = 0,
) -> uuid.UUID:
    async with maker() as session:
        job = ProcessingJob(
            id=uuid.uuid4(),
            video_id=video_id,
            job_type=JobType.pipeline,
            status=status,
            priority=priority,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            leased_by="stale-worker" if lease_expires_at else None,
            lease_expires_at=lease_expires_at,
            created_at=datetime.now(UTC) + timedelta(seconds=created_offset_s),
        )
        session.add(job)
        await session.commit()
        return job.id


async def _claim(
    maker: async_sessionmaker[AsyncSession], worker_id: str = "w1"
) -> ProcessingJob | None:
    """Drive the router's claim endpoint logic through a real session."""
    from unittest.mock import MagicMock

    from app.routers.jobs import JobClaimRequest, claim_job

    async with maker() as session:
        response = MagicMock()
        user = MagicMock(spec=User)
        result = await claim_job(JobClaimRequest(worker_id=worker_id), response, session, user)
        await session.commit()
    if result is None:
        return None
    async with maker() as session:
        row = await session.execute(select(ProcessingJob).where(ProcessingJob.id == result.id))
        return row.scalar_one()


async def test_claim_orders_by_priority_then_age(db: async_sessionmaker[AsyncSession]) -> None:
    video_id = await _seed_video(db)
    low = await _seed_job(db, video_id, priority=0, created_offset_s=0)
    high = await _seed_job(db, video_id, priority=10, created_offset_s=5)

    first = await _claim(db)
    second = await _claim(db)
    assert first is not None and second is not None
    assert first.id == high and second.id == low
    assert first.status == JobStatus.running
    assert first.leased_by == "w1"
    assert first.attempt_count == 1
    assert first.lease_expires_at is not None


async def test_claim_returns_none_when_empty(db: async_sessionmaker[AsyncSession]) -> None:
    assert await _claim(db) is None


async def test_concurrent_claims_never_double_claim(
    db: async_sessionmaker[AsyncSession],
) -> None:
    video_id = await _seed_video(db)
    job_ids = {await _seed_job(db, video_id) for _ in range(3)}

    results = await asyncio.gather(*[_claim(db, worker_id=f"w{i}") for i in range(5)])
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 3
    assert {c.id for c in claimed} == job_ids
    # Each job claimed exactly once.
    assert len({c.id for c in claimed}) == len(claimed)


async def test_expired_lease_is_reclaimable(db: async_sessionmaker[AsyncSession]) -> None:
    video_id = await _seed_video(db)
    stale = await _seed_job(
        db,
        video_id,
        status=JobStatus.running,
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    claimed = await _claim(db, worker_id="w2")
    assert claimed is not None and claimed.id == stale
    assert claimed.leased_by == "w2"
    assert claimed.attempt_count == 2


async def test_exhausted_attempts_swept_to_failed(
    db: async_sessionmaker[AsyncSession],
) -> None:
    video_id = await _seed_video(db)
    exhausted = await _seed_job(
        db,
        video_id,
        status=JobStatus.running,
        attempt_count=3,
        max_attempts=3,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    assert await _claim(db) is None
    async with db() as session:
        row = (
            await session.execute(select(ProcessingJob).where(ProcessingJob.id == exhausted))
        ).scalar_one()
    assert row.status == JobStatus.failed
    assert row.leased_by is None
    assert "attempts exhausted" in (row.error_message or "")


async def test_heartbeat_extends_lease_and_merges_progress(
    db: async_sessionmaker[AsyncSession],
) -> None:
    from unittest.mock import MagicMock

    from app.routers.jobs import JobHeartbeatRequest, heartbeat_job

    video_id = await _seed_video(db)
    await _seed_job(db, video_id)
    claimed = await _claim(db, worker_id="w1")
    assert claimed is not None
    old_expiry = claimed.lease_expires_at

    async with db() as session:
        await heartbeat_job(
            claimed.id,
            JobHeartbeatRequest(worker_id="w1", progress={"ingest": {"status": "succeeded"}}),
            session,
            MagicMock(spec=User),
        )
        await session.commit()
        await heartbeat_job(
            claimed.id,
            JobHeartbeatRequest(worker_id="w1", progress={"detect": {"status": "started"}}),
            session,
            MagicMock(spec=User),
        )
        await session.commit()

    async with db() as session:
        row = (
            await session.execute(select(ProcessingJob).where(ProcessingJob.id == claimed.id))
        ).scalar_one()
    assert row.progress == {
        "ingest": {"status": "succeeded"},
        "detect": {"status": "started"},
    }
    assert row.lease_expires_at is not None and old_expiry is not None
    assert row.lease_expires_at >= old_expiry


async def test_heartbeat_rejects_other_workers(
    db: async_sessionmaker[AsyncSession],
) -> None:
    from unittest.mock import MagicMock

    from app.routers.jobs import JobHeartbeatRequest, heartbeat_job
    from fastapi import HTTPException

    video_id = await _seed_video(db)
    await _seed_job(db, video_id)
    claimed = await _claim(db, worker_id="w1")
    assert claimed is not None

    async with db() as session:
        with pytest.raises(HTTPException) as exc:
            await heartbeat_job(
                claimed.id,
                JobHeartbeatRequest(worker_id="intruder"),
                session,
                MagicMock(spec=User),
            )
    assert exc.value.status_code == 409
