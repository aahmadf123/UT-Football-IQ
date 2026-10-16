"""Nightly scheduler tests (real Postgres): export → threshold → enqueue.

Covers the learning-loop tick: corrections export via the shared service,
train-job enqueue only at/after TRAINING_MIN_NEW_LABELS, per-day idempotence
for train/workload_rollup, and that promotion is never touched (the tick
only creates queued jobs).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import (
    CoachCorrection,
    CorrectionType,
    JobStatus,
    JobType,
    Label,
    ProcessingJob,
    User,
    UserRole,
)
from app.scheduler import _ADVISORY_LOCK_KEY, _acquire_lock, run_nightly_tick
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_TABLE_NAMES = [
    "users",
    "training_datasets",
    "model_versions",
    "videos",
    "field_calibrations",
    "clips",
    "processing_jobs",
    "players",
    "tracklets",
    "labels",
    "coach_corrections",
]
_TABLES = [Base.metadata.tables[n] for n in _TABLE_NAMES]

_ENUM_TYPES = (
    "correction_type",
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
async def db(tmp_path, monkeypatch) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    # Schema-local tables keep repeated runs independent even when the migrated
    # development/test database already contains human labels and jobs.
    schema = f"scheduler_{uuid.uuid4().hex}"
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTO_TRAIN_ENABLED", "false")
    get_settings.cache_clear()
    engine = create_async_engine(
        get_settings().database_url,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
        execution_options={"schema_translate_map": {None: schema}},
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def _seed_coach(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as session:
        coach = User(
            id=uuid.uuid4(),
            email=f"sched-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Sched Coach",
            role=UserRole.coach,
        )
        session.add(coach)
        await session.commit()
        return coach.id


async def _seed_human_labels(maker: async_sessionmaker[AsyncSession], count: int) -> None:
    async with maker() as session:
        for _ in range(count):
            session.add(
                Label(
                    id=uuid.uuid4(),
                    clip_id=None,
                    label_type="formation",
                    label_value={"name": "trips_right"},
                    source="human",
                )
            )
        await session.commit()


async def _jobs_of_type(
    maker: async_sessionmaker[AsyncSession], job_type: JobType
) -> list[ProcessingJob]:
    async with maker() as session:
        rows = await session.execute(
            select(ProcessingJob).where(ProcessingJob.job_type == job_type)
        )
        return list(rows.scalars().all())


async def test_tick_enqueues_rollup_once_per_day(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        first = await run_nightly_tick(session)
        await session.commit()
    async with db() as session:
        second = await run_nightly_tick(session)
        await session.commit()

    assert first["workload_rollup_job_id"] is not None
    assert second["workload_rollup_job_id"] is None  # same-day idempotence
    rollups = await _jobs_of_type(db, JobType.workload_rollup)
    assert len(rollups) == 1
    assert rollups[0].status == JobStatus.queued


async def test_unclaimed_rollups_coalesce_without_losing_days(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Regression: with no full worker attached, every nightly tick used to add
    # another queued rollup (60 accumulated in production). They must now fold
    # into one pending job that still owes every missed day.
    async with db() as session:
        for days_ago, date in ((5, "2026-01-01"), (4, "2026-01-02"), (3, "2026-01-03")):
            session.add(
                ProcessingJob(
                    id=uuid.uuid4(),
                    job_type=JobType.workload_rollup,
                    status=JobStatus.queued,
                    priority=0,
                    created_at=datetime.now(UTC) - timedelta(days=days_ago),
                    input_artifacts={"date": date, "trigger": "scheduler"},
                )
            )
        await session.commit()
    async with db() as session:
        summary = await run_nightly_tick(session)
        await session.commit()

    assert summary["merged_rollups"] == 2
    rollups = await _jobs_of_type(db, JobType.workload_rollup)
    pending = [r for r in rollups if r.status == JobStatus.queued]
    assert len(pending) == 1
    assert str(pending[0].id) == summary["workload_rollup_job_id"]
    dates = pending[0].input_artifacts["dates"]
    assert dates[:3] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert len(dates) == 4  # plus yesterday
    assert sum(r.status == JobStatus.cancelled for r in rollups) == 2


async def test_repeat_tick_after_old_keeper_claimed_does_not_duplicate_rollup(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Regression: yesterday is folded into an older keeper (created days ago).
    # Once a worker claims it, a repeat tick must still see yesterday covered.
    async with db() as session:
        session.add(
            ProcessingJob(
                id=uuid.uuid4(),
                job_type=JobType.workload_rollup,
                status=JobStatus.queued,
                priority=0,
                created_at=datetime.now(UTC) - timedelta(days=3),
                input_artifacts={"date": "2026-01-01", "trigger": "scheduler"},
            )
        )
        await session.commit()
    async with db() as session:
        first = await run_nightly_tick(session)
        await session.commit()
    async with db() as session:
        keeper = await session.get(ProcessingJob, uuid.UUID(str(first["workload_rollup_job_id"])))
        assert keeper is not None
        keeper.status = JobStatus.running
        await session.commit()
    async with db() as session:
        second = await run_nightly_tick(session)
        await session.commit()

    assert second["workload_rollup_job_id"] is None
    rollups = await _jobs_of_type(db, JobType.workload_rollup)
    assert len(rollups) == 1


async def test_tick_below_threshold_does_not_train(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINING_MIN_NEW_LABELS", "5")
    # The db fixture already read (and cached) Settings, so drop the cache
    # to make this override the value the tick actually sees.
    get_settings.cache_clear()
    await _seed_human_labels(db, 3)
    async with db() as session:
        summary = await run_nightly_tick(session)
        await session.commit()
    assert summary["new_human_labels"] == 3
    assert summary["train_job_id"] is None
    assert await _jobs_of_type(db, JobType.train) == []


async def test_tick_at_threshold_keeps_automatic_training_off_by_default(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRAINING_MIN_NEW_LABELS", "5")
    # The db fixture already read (and cached) Settings, so drop the cache
    # to make this override the value the tick actually sees.
    get_settings.cache_clear()
    await _seed_human_labels(db, 6)
    async with db() as session:
        summary = await run_nightly_tick(session)
        await session.commit()
    assert summary["train_job_id"] is None
    assert summary["training_enabled"] is False
    async with db() as session:
        again = await run_nightly_tick(session)
        await session.commit()
    assert again["train_job_id"] is None  # same-day idempotence
    trains = await _jobs_of_type(db, JobType.train)
    assert trains == []


async def test_advisory_lock_released_after_commit(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Regression: the tick lock must be transaction-scoped. A session-scoped
    # pg_advisory_lock would survive db.commit() (the connection returns to the
    # pool still holding it), so no advisory lock for our key may remain once a
    # committed transaction has ended.
    async with db() as session:
        assert await _acquire_lock(session) is True
        await session.commit()

    async with db() as checker:
        remaining = (
            await checker.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND ((classid::bigint << 32) | objid::bigint) = :key"
                ),
                {"key": _ADVISORY_LOCK_KEY},
            )
        ).scalar()
        await checker.rollback()
    assert remaining == 0

    # And a fresh tick can re-acquire it (not wedged).
    async with db() as session:
        assert await _acquire_lock(session) is True
        await session.commit()


async def test_tick_exports_corrections_via_service(
    db: async_sessionmaker[AsyncSession],
) -> None:
    from app.models import Clip, Video, VideoStatus

    coach_id = await _seed_coach(db)
    async with db() as session:
        video = Video(
            id=uuid.uuid4(),
            filename="sched.mp4",
            storage_uri="local://raw-video/raw/sched.mp4",
            status=VideoStatus.uploaded,
            uploaded_by=coach_id,
        )
        session.add(video)
        clip = Clip(id=uuid.uuid4(), video_id=video.id, start_time=0.0, end_time=5.0)
        session.add(clip)
        session.add(
            CoachCorrection(
                id=uuid.uuid4(),
                clip_id=clip.id,
                correction_type=CorrectionType.formation_tag,
                original_value={"name": "wrong"},
                corrected_value={"name": "trips_right"},
                corrected_by=coach_id,
                training_eligible=True,
            )
        )
        await session.commit()

    async with db() as session:
        summary = await run_nightly_tick(session)
        await session.commit()

    assert summary["exported_corrections"] == 1
    assert summary["training_dataset_id"] is not None
    async with db() as session:
        rows = await session.execute(select(CoachCorrection))
        correction = rows.scalars().one()
        assert correction.exported_as_label is True
        labels = (
            (await session.execute(select(Label).where(Label.source == "human"))).scalars().all()
        )
        assert len(labels) == 1
