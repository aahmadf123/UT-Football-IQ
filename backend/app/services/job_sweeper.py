"""Reclaim jobs whose worker died holding the lease.

``POST /jobs/claim`` sweeps before it claims, which is enough while workers are
polling. It is *not* enough when they stop: a job whose lease expired sits in
``running`` forever if nothing calls ``/claim`` again -- so a crashed worker
during an off-hours batch leaves rows that look busy on the dashboard and are
never retried. The nightly tick runs the same sweep on a timer so the queue
converges even with no workers attached.

Both passes are idempotent, so running them from two places (and twice in the
same minute) is harmless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobStatus, ProcessingJob

log = structlog.get_logger(__name__)

#: Written to ``error_message`` when a job runs out of attempts.
LEASE_EXHAUSTED_MESSAGE = "lease expired; attempts exhausted"


@dataclass(frozen=True)
class SweepResult:
    """How many rows each pass touched."""

    failed: int
    requeued: int

    @property
    def total(self) -> int:
        return self.failed + self.requeued


async def sweep_expired_leases(db: AsyncSession, now: datetime | None = None) -> SweepResult:
    """Fail or requeue every ``running`` job whose lease has expired.

    * Attempts exhausted -> ``failed``, with the lease cleared. This is the pass
      ``/claim`` has always done.
    * Attempts remaining -> back to ``queued``, with the lease cleared. ``/claim``
      would pick these up anyway (it treats an expired lease as claimable), but
      leaving them marked ``running`` misreports the queue to the dashboard and
      to the workload-gating thresholds.

    The caller owns the transaction; nothing here commits.
    """
    now = now or datetime.now(UTC)

    expired = (
        ProcessingJob.status == JobStatus.running,
        ProcessingJob.lease_expires_at.is_not(None),
        ProcessingJob.lease_expires_at < now,
    )

    failed = await db.execute(
        update(ProcessingJob)
        .where(*expired, ProcessingJob.attempt_count >= ProcessingJob.max_attempts)
        .values(
            status=JobStatus.failed,
            error_message=LEASE_EXHAUSTED_MESSAGE,
            finished_at=now,
            leased_by=None,
            lease_expires_at=None,
        )
    )

    requeued = await db.execute(
        update(ProcessingJob)
        .where(*expired, ProcessingJob.attempt_count < ProcessingJob.max_attempts)
        .values(
            status=JobStatus.queued,
            leased_by=None,
            lease_expires_at=None,
        )
    )

    result = SweepResult(failed=_rowcount(failed), requeued=_rowcount(requeued))
    if result.total:
        log.info("job_lease_sweep", failed=result.failed, requeued=result.requeued)
    return result


def _rowcount(result: object) -> int:
    """Rows an UPDATE touched.

    ``AsyncSession.execute`` is typed as returning ``Result``, but an UPDATE
    always yields a ``CursorResult``, which is where ``rowcount`` lives. Guarded
    rather than cast so a mocked session in the unit tests -- which returns a
    plain ``MagicMock`` -- reports 0 instead of raising.
    """
    if isinstance(result, CursorResult):
        return int(result.rowcount or 0)
    return 0
