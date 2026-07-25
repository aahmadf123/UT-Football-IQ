"""Coach-tagged play-call id on clips for cross-regime pairing (Issue #150).

Adds a nullable, indexed ``play_call_id`` (String(100)) column to ``clips``.
Coaching staff tag the practice (``drone_follow``) clip and the game
(``fixed_sideline``) clip of the same play with the *same* play-call code; two
clips are a "paired play" when they share a non-null ``play_call_id``. The
practice<->game cross-regime self-distillation trainer aligns the game
pseudo-labels onto the practice clip via this key (§5.14).

No backfill: the column is nullable and only populated when a coach tags a
clip, so pre-existing rows correctly remain ``NULL`` (unpaired).

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clips",
        sa.Column("play_call_id", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_clips_play_call_id", "clips", ["play_call_id"])


def downgrade() -> None:
    op.drop_index("ix_clips_play_call_id", table_name="clips")
    op.drop_column("clips", "play_call_id")
