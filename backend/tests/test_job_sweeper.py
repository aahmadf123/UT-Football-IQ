"""Lease sweeping — the pass that keeps a dead worker from wedging the queue.

Exercised against the real database, because the behaviour under test *is* the
SQL: two UPDATEs partitioned on ``attempt_count`` vs ``max_attempts``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import JobStatus, JobType, PipelineMode, ProcessingJob
from app.services.job_sweeper import LEASE_EXHAUSTED_MESSAGE, sweep_expired_leases
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _dependency_closure(*table_names: str) -> list[str]:
    """Every table reachable from ``table_names`` by following foreign keys.

    This suite only writes ``processing_jobs`` rows, but that table pulls in
    videos, clips, model_versions, training_datasets and field_calibrations
    through its FK graph. Computing the closure keeps the fixture correct as
    the schema grows, instead of failing with "relation ... does not exist" the
    next time somebody adds a column.
    """
    seen: set[str] = set()
    stack = list(table_names)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys)
    return [t.name for t in Base.metadata.sorted_tables if t.name in seen]


_TABLE_NAMES = _dependency_closure("processing_jobs")
_TABLES = [Base.metadata.tables[n] for n in _TABLE_NAMES]

#: Enum types owned by those tables. Dropped alongside them so a later suite
#: recreating the same tables does not trip over a leftover type.
_ENUM_TYPES = sorted(
    {
        column.type.name
        for table in _TABLES
        for column in table.columns
        if getattr(column.type, "name", None) and hasattr(column.type, "enums")
    }
)


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        existing: set[str] = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    created = [t for t in _TABLES if t.name not in existing]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=created))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if created:
            async with engine.begin() as conn:
                for table in reversed(created):
                    await conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
            for enum_name in _ENUM_TYPES:
                try:
                    async with engine.begin() as conn:
                        await conn.exec_driver_sql(f"DROP TYPE IF EXISTS {enum_name}")
                except Exception:  # noqa: S110 — best-effort teardown
                    pass
        await engine.dispose()


def _job(
    *,
    status: JobStatus,
    lease_expires_at: datetime | None,
    attempt_count: int,
    max_attempts: int = 3,
) -> ProcessingJob:
    return ProcessingJob(
        id=uuid.uuid4(),
        job_type=JobType.detect,
        status=status,
        priority=0,
        pipeline_mode=PipelineMode.nightly,
        leased_by="worker-1" if lease_expires_at else None,
        lease_expires_at=lease_expires_at,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_expired_lease_with_attempts_left_is_requeued(
    db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    job = _job(
        status=JobStatus.running, lease_expires_at=now - timedelta(minutes=5), attempt_count=1
    )
    async with db() as session:
        session.add(job)
        await session.commit()

    async with db() as session:
        result = await sweep_expired_leases(session, now)
        await session.commit()
        assert result.requeued == 1
        assert result.failed == 0

    async with db() as session:
        refreshed = (
            await session.execute(select(ProcessingJob).where(ProcessingJob.id == job.id))
        ).scalar_one()
        assert refreshed.status == JobStatus.queued
        # The lease must be cleared, otherwise the row still looks owned.
        assert refreshed.leased_by is None
        assert refreshed.lease_expires_at is None
        # Requeue is not a retry: the attempt is only spent on the next claim.
        assert refreshed.attempt_count == 1


@pytest.mark.asyncio
async def test_expired_lease_with_attempts_exhausted_fails(
    db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    job = _job(
        status=JobStatus.running,
        lease_expires_at=now - timedelta(minutes=5),
        attempt_count=3,
        max_attempts=3,
    )
    async with db() as session:
        session.add(job)
        await session.commit()

    async with db() as session:
        result = await sweep_expired_leases(session, now)
        await session.commit()
        assert result.failed == 1
        assert result.requeued == 0

    async with db() as session:
        refreshed = (
            await session.execute(select(ProcessingJob).where(ProcessingJob.id == job.id))
        ).scalar_one()
        assert refreshed.status == JobStatus.failed
        assert refreshed.error_message == LEASE_EXHAUSTED_MESSAGE
        assert refreshed.finished_at is not None
        assert refreshed.leased_by is None


@pytest.mark.asyncio
async def test_live_lease_is_left_alone(db: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    job = _job(
        status=JobStatus.running,
        lease_expires_at=now + timedelta(minutes=5),
        attempt_count=1,
    )
    async with db() as session:
        session.add(job)
        await session.commit()

    async with db() as session:
        result = await sweep_expired_leases(session, now)
        await session.commit()
        assert result.total == 0

    async with db() as session:
        refreshed = (
            await session.execute(select(ProcessingJob).where(ProcessingJob.id == job.id))
        ).scalar_one()
        assert refreshed.status == JobStatus.running
        assert refreshed.leased_by == "worker-1"


@pytest.mark.asyncio
async def test_queued_and_terminal_jobs_are_untouched(
    db: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    queued = _job(status=JobStatus.queued, lease_expires_at=None, attempt_count=0)
    # A stale lease on a job that already succeeded must not be resurrected.
    succeeded = _job(
        status=JobStatus.succeeded,
        lease_expires_at=now - timedelta(hours=1),
        attempt_count=1,
    )
    async with db() as session:
        session.add_all([queued, succeeded])
        await session.commit()

    async with db() as session:
        result = await sweep_expired_leases(session, now)
        await session.commit()
        assert result.total == 0

    async with db() as session:
        rows = {
            row.id: row.status
            for row in (
                await session.execute(
                    select(ProcessingJob).where(ProcessingJob.id.in_([queued.id, succeeded.id]))
                )
            )
            .scalars()
            .all()
        }
        assert rows[queued.id] == JobStatus.queued
        assert rows[succeeded.id] == JobStatus.succeeded


@pytest.mark.asyncio
async def test_sweep_is_idempotent(db: async_sessionmaker[AsyncSession]) -> None:
    # It runs from two places (every claim, and the nightly tick), so a second
    # pass over already-swept rows must be a no-op rather than double-counting.
    now = datetime.now(UTC)
    job = _job(
        status=JobStatus.running, lease_expires_at=now - timedelta(minutes=5), attempt_count=1
    )
    async with db() as session:
        session.add(job)
        await session.commit()

    async with db() as session:
        first = await sweep_expired_leases(session, now)
        await session.commit()
    async with db() as session:
        second = await sweep_expired_leases(session, now)
        await session.commit()

    assert first.total == 1
    assert second.total == 0
