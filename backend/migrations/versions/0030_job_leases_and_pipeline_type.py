"""DB-as-queue: job lease columns, per-stage progress, and new job types.

The GPU worker now claims jobs straight from ``processing_jobs`` (POST
/api/v1/jobs/claim, FOR UPDATE SKIP LOCKED) instead of a Cloudflare Queue,
so the table needs lease bookkeeping and a claim-ordered partial index.

``job_type`` gains the orchestrated ``pipeline`` type (full stage chain in
one job), the stage types that previously existed only inside queue
messages (``reid``, ``events``, ``pressure``, ``workload_rollup``), and the
scheduled ``train`` type. ``ADD VALUE IF NOT EXISTS`` runs in an autocommit
block because Postgres refuses enum additions inside a transaction whose
later statements use them; removal on downgrade is a Postgres no-op by
design (harmless — unused values are inert).

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_JOB_TYPES = ("pipeline", "reid", "events", "pressure", "workload_rollup", "train")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_JOB_TYPES:
            op.execute(f"ALTER TYPE job_type ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column("processing_jobs", sa.Column("leased_by", sa.String(length=128), nullable=True))
    op.add_column(
        "processing_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column("processing_jobs", sa.Column("progress", sa.JSON(), nullable=True))

    op.create_index(
        "ix_processing_jobs_claim",
        "processing_jobs",
        [sa.text("priority DESC"), "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_claim", table_name="processing_jobs")
    op.drop_column("processing_jobs", "progress")
    op.drop_column("processing_jobs", "max_attempts")
    op.drop_column("processing_jobs", "attempt_count")
    op.drop_column("processing_jobs", "lease_expires_at")
    op.drop_column("processing_jobs", "leased_by")
    # Enum values added in upgrade() are left in place: Postgres cannot drop
    # enum values, and inert values are harmless.
