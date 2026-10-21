"""Effective dates and per-session practice roster overrides."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_roster_context"
down_revision: str | None = "0008_workspace_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "roster_assignments", sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("roster_assignments", sa.Column("effective_from", sa.Date(), nullable=True))
    op.add_column("roster_assignments", sa.Column("effective_until", sa.Date(), nullable=True))
    op.create_foreign_key(
        "roster_assignments_session_id_fkey",
        "roster_assignments",
        "football_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_roster_assignments_session_id", "roster_assignments", ["session_id"])
    op.drop_constraint("uq_roster_assignment_scope", "roster_assignments", type_="unique")
    op.create_unique_constraint(
        "uq_roster_assignment_scope",
        "roster_assignments",
        ["player_id", "team", "season", "unit", "session_id"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # Session overrides cannot be discarded silently to restore a global row.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM roster_assignments
               GROUP BY player_id, team, season, unit HAVING count(*) > 1)
               THEN RAISE EXCEPTION 'Consolidate session roster overrides before downgrade';
               END IF; END $$;""")
    op.drop_constraint("uq_roster_assignment_scope", "roster_assignments", type_="unique")
    op.create_unique_constraint(
        "uq_roster_assignment_scope", "roster_assignments", ["player_id", "team", "season", "unit"]
    )
    op.drop_index("ix_roster_assignments_session_id", table_name="roster_assignments")
    op.drop_constraint(
        "roster_assignments_session_id_fkey", "roster_assignments", type_="foreignkey"
    )
    for column in ("effective_until", "effective_from", "session_id"):
        op.drop_column("roster_assignments", column)
