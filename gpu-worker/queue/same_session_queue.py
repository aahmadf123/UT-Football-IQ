"""Job priority buckets shared by the pipeline, router, and dispatcher.

The transport is the backend's ``processing_jobs`` table: a job row claimed
with ``FOR UPDATE SKIP LOCKED`` *is* the queue entry. These constants are the
values written to ``processing_jobs.priority``, and the model router keys its
same-session / nightly variant selection off them.

Priority levels:
  SAME_SESSION_PRIORITY = 10  — period-break / real-time jobs. Must fit the
      5-10 minute coaching feedback window, so the router picks fast variants.
  NIGHTLY_PRIORITY      = 0   — full-session batch processing. Heavier
      variants are allowed.
"""

from __future__ import annotations

# Priority values used in the processing_jobs.priority column.
SAME_SESSION_PRIORITY: int = 10
NIGHTLY_PRIORITY: int = 0

__all__ = ["NIGHTLY_PRIORITY", "SAME_SESSION_PRIORITY"]
