"""Nightly scheduler — the set-and-forget half of the learning loop.

Once per UTC day the tick:

  1. Reclaims jobs whose worker died holding the lease.
  2. Exports new coach corrections to Labels + a TrainingDataset snapshot
     (same service the manual /api/v1/corrections/export endpoint uses).
  3. When the accumulated human-label count since the last training job
     reaches ``TRAINING_MIN_NEW_LABELS``, enqueues a ``train`` ProcessingJob
     for the GPU worker. Training output registers as ``experimental``;
     PROMOTION REMAINS A HUMAN DECISION (POST /mlops/models/{id}/promote) —
     the scheduler never promotes.
  4. Enqueues the nightly ``workload_rollup`` job.

**Two ways to fire it.** The in-process asyncio loop below polls every five
minutes and acts on the hour, which is right for a long-lived process. It is
useless on a platform that suspends an idle container — the process is simply
not running at 08:00 UTC to notice. So the tick is also exposed as
``POST /internal/scheduler/tick`` (see ``app.routers.internal``) for an external
scheduler such as a Cloudflare cron trigger to call. Set ``SCHEDULER_ENABLED=0``
when using the external trigger so the two do not both fire.

Either way, running twice is safe: a Postgres *transaction-scoped* advisory lock
admits one caller at a time, and every step is individually idempotent. The lock
is bound to the tick's transaction and released on commit/rollback — a
session-scoped lock would leak, because after ``db.commit()`` returns the
connection to the pool a manual unlock could run on a different connection and
never release the original, wedging every later tick.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import JobStatus, JobType, Label, PipelineMode, ProcessingJob

log = structlog.get_logger(__name__)

#: Advisory lock key for the nightly tick (arbitrary but stable).
_ADVISORY_LOCK_KEY = 987_654_321

_CHECK_INTERVAL_SECONDS = 300


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
    """One nightly tick: sweep leases, export corrections, enqueue train + rollup.

    Returns a summary dict (also used by tests). The caller owns the commit.
    """
    from app.services.corrections_export import export_corrections
    from app.services.job_sweeper import sweep_expired_leases

    now = datetime.now(UTC)
    summary: dict[str, object] = {"ran_at": now.isoformat()}

    # Runs first: a job stuck in ``running`` behind a dead worker's lease is
    # invisible to ``/claim`` if nothing is polling, and it inflates the
    # workload-gating counters that decide whether new uploads are accepted.
    sweep = await sweep_expired_leases(db, now)
    summary["swept_failed"] = sweep.failed
    summary["swept_requeued"] = sweep.requeued

    export = await export_corrections(db)
    summary["exported_corrections"] = export.exported_count
    summary["training_dataset_id"] = (
        str(export.training_dataset_id) if export.training_dataset_id else None
    )

    new_labels = await _new_human_labels_since_last_train(db)
    summary["new_human_labels"] = new_labels
    min_new_labels = get_settings().training_min_new_labels
    if new_labels >= min_new_labels and not await _already_ran_today(db, JobType.train, now):
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


async def run_locked_tick(db: AsyncSession) -> dict[str, object] | None:
    """Run one tick under the advisory lock, committing on success.

    Returns the summary, or ``None`` when another caller already holds the lock
    (which is the normal outcome for the losing instance, not an error). Shared
    by the in-process loop and the external ``/internal/scheduler/tick`` route so
    both get identical locking and error semantics.
    """
    if not await _acquire_lock(db):
        log.info("scheduler_tick_skipped_locked")
        return None
    try:
        # Idempotence inside the hour: the per-day job checks in
        # run_nightly_tick keep repeat ticks from double-enqueueing;
        # corrections export is naturally idempotent (flag-driven).
        summary = await run_nightly_tick(db)
        await db.commit()
        log.info("scheduler_tick_complete", **{k: v for k, v in summary.items()})
        return summary
    except Exception as exc:
        await db.rollback()
        log.error("scheduler_tick_failed", error=str(exc))
        raise


async def _tick_if_due() -> None:
    from app.database import AsyncSessionLocal

    now = datetime.now(UTC)
    if now.hour != get_settings().scheduler_hour_utc:
        return
    async with AsyncSessionLocal() as db:
        # The lock is taken inside this transaction and released by the
        # commit/rollback in run_locked_tick — no finally-unlock (which would
        # run on a possibly-different pooled connection after commit and leak
        # the lock). A failure propagates to scheduler_loop, which logs it and
        # keeps looping; run_locked_tick has already logged the detail.
        await run_locked_tick(db)


async def scheduler_loop() -> None:
    """Long-lived loop; started/cancelled by the app lifespan."""
    settings = get_settings()
    log.info(
        "scheduler_started",
        hour_utc=settings.scheduler_hour_utc,
        min_new_labels=settings.training_min_new_labels,
    )
    while True:
        try:
            await _tick_if_due()
        except Exception as exc:  # the loop must survive anything
            log.error("scheduler_loop_error", error=str(exc))
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler() -> asyncio.Task[None] | None:
    """Create the scheduler task if enabled; caller cancels it on shutdown."""
    if not get_settings().in_process_scheduler_enabled:
        log.info("scheduler_disabled")
        return None
    return asyncio.create_task(scheduler_loop(), name="nightly-scheduler")
