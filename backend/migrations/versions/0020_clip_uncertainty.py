"""Active-learning uncertainty columns on clips (Issues #145 / #146).

Revision ID: 0020_clip_uncertainty
Revises: 0019
Create Date: 2026-05-30

Adds the clip-level calibrated-uncertainty signal that drives the active-learning
annotation queue:

- ``clips.uncertainty_score``       FLOAT NULL  — normalized entropy in [0, 1]
                                                  (higher = review first); NULL
                                                  means "not yet scored".
- ``clips.uncertainty_calibrated``  BOOL NOT NULL DEFAULT false — whether the
                                                  contributing model outputs were
                                                  calibrated (#146); an
                                                  uncalibrated score is never
                                                  presented to a coach as a
                                                  trusted confidence.

An index on ``uncertainty_score`` supports the queue's "highest entropy first"
ordering. Both columns are additive and nullable/defaulted so existing rows
upgrade cleanly, and the downgrade removes exactly what the upgrade added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_clip_uncertainty"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply schema changes."""
    op.add_column("clips", sa.Column("uncertainty_score", sa.Float(), nullable=True))
    op.add_column(
        "clips",
        sa.Column(
            "uncertainty_calibrated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index("ix_clips_uncertainty_score", "clips", ["uncertainty_score"])


def downgrade() -> None:
    """Revert schema changes."""
    op.drop_index("ix_clips_uncertainty_score", table_name="clips")
    op.drop_column("clips", "uncertainty_calibrated")
    op.drop_column("clips", "uncertainty_score")
