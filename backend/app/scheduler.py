"""In-process nightly scheduler — the set-and-forget half of the learning loop.

Runs as an asyncio task started from the FastAPI lifespan (works identically
in local compose and cloud deployments — no Cloudflare cron required). Once
per UTC day at ``SCHEDULER_HOUR_UTC``:

  1. Export new coach corrections to Labels + a TrainingDataset snapshot
     (same service the manual /api/v1/corrections/export endpoint uses).
  2. When the accumulated human-label count since the last training job
     reaches ``TRAINING_MIN_NEW_LABELS``, enqueue a ``train`` ProcessingJob
     for the GPU worker. Training output registers as ``experimental``;
     PROMOTION REMAINS A HUMAN DECISION (POST /mlops/models/{id}/promote) —
     the scheduler never promotes.
  3. Enqueue the nightly ``workload_rollup`` job (previously the lone
     Cloudflare cron responsibility).

Multi-instance deployments are safe: a Postgres *transaction-scoped* advisory
lock makes exactly one instance run the tick; the others skip. It is bound to
the tick's transaction and released automatically on commit/rollback — a
session-scoped lock would leak, because after ``db.commit()`` returns the
connection to the pool a manual unlock could run on a different connection and
never release the original, wedging every later tick. ``SCHEDULER_ENABLED=0``
disables the loop entirely (e.g. one-off maintenance containers).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobStatus, JobType, Label, PipelineMode, ProcessingJob

log = structlog.get_logger(__name__)

#: Advisory lock key for the nightly tick (arbitrary but stable).
_ADVISORY_LOCK_KEY = 987_654_321

_CHECK_INTERVAL_SECONDS = 300


def _enabled() -> bool:
    explicit = os.environ.get("SCHEDULER_ENABLED", "").strip().lower()
    if explicit:
        return explicit not in {"0", "false", "no"}
    # Default on, except under the test environment (hundreds of TestClient
    # lifespans would otherwise each spawn a loop task).
    return os.environ.get("ENVIRONMENT", "").strip().lower() != "test"


def _scheduled_hour() -> int:
    try:
        return max(0, min(23, int(os.environ.get("SCHEDULER_HOUR_UTC", "8"))))
    except ValueError:
        return 8


def _min_new_labels() -> int:
    try:
        return max(1, int(os.environ.get("TRAINING_MIN_NEW_LABELS", "200")))
    except ValueError:
        return 200


async def _acquire_lock(db: AsyncSession) -> bool:
    # Transaction-scoped: Postgres releases it when this session's transaction
    # commits or rolls back, so there is no manual unlock to leak onto a
    # pooled connection. Acquired inside the same transaction that
    # run_nightly_tick writes and _tick_if_due commits.
    result = await db.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}
    )
    return bool(result.scalar())


async def _already_ran_today(db: AsyncSession, job_type: JobType, now: datetime) -> bool:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = (
        await db.execute(
            select(func.count())
            .select_from(ProcessingJob)
            .where(ProcessingJob.job_type == job_type, ProcessingJob.created_at >= day_start)
        )
    ).scalar_one()
    return bool(count)


async def _new_human_labels_since_last_train(db: AsyncSession) -> int:
    last_train_at = (
        await db.execute(
            select(func.max(ProcessingJob.created_at)).where(
                ProcessingJob.job_type == JobType.train
            )
        )
    ).scalar_one()
    q = select(func.count()).select_from(Label).where(Label.source == "human")
    if last_train_at is not None:
        q = q.where(Label.created_at > last_train_at)
    return int((await db.execute(q)).scalar_one() or 0)


async def run_nightly_tick(db: AsyncSession) -> dict[str, object]:
    """One nightly tick: export corrections, maybe enqueue train, enqueue rollup.

    Returns a summary dict (also used by tests). The caller owns the commit.
    """
    from app.services.corrections_export import export_corrections

    now = datetime.now(UTC)
    summary: dict[str, object] = {"ran_at": now.isoformat()}

    export = await export_corrections(db)
    summary["exported_corrections"] = export.exported_count
    summary["training_dataset_id"] = (
        str(export.training_dataset_id) if export.training_dataset_id else None
    )

    new_labels = await _new_human_labels_since_last_train(db)
    summary["new_human_labels"] = new_labels
    if new_labels >= _min_new_labels() and not await _already_ran_today(db, JobType.train, now):
        train_job = ProcessingJob(
            id=uuid.uuid4(),
            job_type=JobType.train,
            status=JobStatus.queued,
            priority=0,
            pipeline_mode=PipelineMode.nightly,
            training_dataset_id=export.training_dataset_id,
            input_artifacts={
                "trigger": "scheduler",
                "new_human_labels": new_labels,
            },
        )
        db.add(train_job)
        summary["train_job_id"] = str(train_job.id)
        log.info("scheduler_train_enqueued", job_id=str(train_job.id), new_labels=new_labels)
    else:
        summary["train_job_id"] = None

    if not await _already_ran_today(db, JobType.workload_rollup, now):
        rollup_date = (now.date() - timedelta(days=1)).isoformat()
        rollup_job = ProcessingJob(
            id=uuid.uuid4(),
            job_type=JobType.workload_rollup,
            status=JobStatus.queued,
            priority=0,
            pipeline_mode=PipelineMode.nightly,
            input_artifacts={"date": rollup_date, "trigger": "scheduler"},
        )
        db.add(rollup_job)
        summary["workload_rollup_job_id"] = str(rollup_job.id)
    else:
        summary["workload_rollup_job_id"] = None

    await db.flush()
    return summary


async def _tick_if_due() -> None:
    from app.database import AsyncSessionLocal

    now = datetime.now(UTC)
    if now.hour != _scheduled_hour():
        return
    async with AsyncSessionLocal() as db:
        # The lock is taken inside this transaction and released by the
        # commit/rollback below — no finally-unlock (which would run on a
        # possibly-different pooled connection after commit and leak the lock).
        if not await _acquire_lock(db):
            return
        try:
            # Idempotence inside the hour: the per-day job checks in
            # run_nightly_tick keep repeat ticks from double-enqueueing;
            # corrections export is naturally idempotent (flag-driven).
            summary = await run_nightly_tick(db)
            await db.commit()
            log.info("scheduler_tick_complete", **{k: v for k, v in summary.items()})
        except Exception as exc:
            await db.rollback()
            log.error("scheduler_tick_failed", error=str(exc))


async def scheduler_loop() -> None:
    """Long-lived loop; started/cancelled by the app lifespan."""
    log.info(
        "scheduler_started",
        hour_utc=_scheduled_hour(),
        min_new_labels=_min_new_labels(),
    )
    while True:
        try:
            await _tick_if_due()
        except Exception as exc:  # the loop must survive anything
            log.error("scheduler_loop_error", error=str(exc))
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler() -> asyncio.Task[None] | None:
    """Create the scheduler task if enabled; caller cancels it on shutdown."""
    if not _enabled():
        log.info("scheduler_disabled")
        return None
    return asyncio.create_task(scheduler_loop(), name="nightly-scheduler")
