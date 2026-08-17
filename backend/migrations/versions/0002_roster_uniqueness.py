"""Roster identity indexes.

Two indexes supporting the 2026 roster ingestion and per-player analytics:

* ``uq_players_jersey_posgroup_active`` — partial unique index on
  ``players (jersey_number, position_group)`` for active rows only. Jersey
  numbers repeat across sides of the ball on a college roster (two #7s — a QB
  and a CB — is normal), so the pair is the smallest unit that identifies a
  player from film. Partial so historical/inactive rows and unnumbered
  walk-ons never block a seed pass.
* ``ix_tracklets_player_id`` — the per-player metrics summary endpoint groups
  tracklets by ``player_id``, which previously had no index.

Revision ID: 0002_roster_uniqueness
Revises: 0001_baseline
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_roster_uniqueness"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_players_jersey_posgroup_active",
        "players",
        ["jersey_number", "position_group"],
        unique=True,
        postgresql_where=sa.text(
            "is_active AND jersey_number IS NOT NULL AND position_group IS NOT NULL"
        ),
    )
    op.create_index(op.f("ix_tracklets_player_id"), "tracklets", ["player_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tracklets_player_id"), table_name="tracklets")
    op.drop_index("uq_players_jersey_posgroup_active", table_name="players")
