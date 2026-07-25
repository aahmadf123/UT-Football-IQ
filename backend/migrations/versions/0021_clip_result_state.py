"""Same-session result tier on clips (Issue #147).

Revision ID: 0021_clip_result_state
Revises: 0020_clip_uncertainty
Create Date: 2026-05-31

Adds the clip-level ``result_state`` that drives the same-session 90-second
feedback loop's "Preliminary" badge and the nightly upgrade:

- ``clips.result_state`` VARCHAR(20) NULL — ``preliminary`` for same-session
  first-pass results, ``final`` once the nightly full-quality path has upgraded
  them in place. NULL means "legacy / unknown" (rows that predate the
  same-session split); such clips are never shown as preliminary.

An index supports the Practice Inbox query that counts clips still awaiting
their nightly upgrade. The column is additive and nullable so existing rows
upgrade cleanly, and the downgrade removes exactly what the upgrade added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_clip_result_state"
down_revision: str | Sequence[str] | None = "0020_clip_uncertainty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply schema changes."""
    op.add_column("clips", sa.Column("result_state", sa.String(length=20), nullable=True))
    op.create_index("ix_clips_result_state", "clips", ["result_state"])


def downgrade() -> None:
    """Revert schema changes."""
    op.drop_index("ix_clips_result_state", table_name="clips")
    op.drop_column("clips", "result_state")
