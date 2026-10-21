"""Persist staff views and scoped roster assignments without unique jerseys."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_workspace_publication"
down_revision: str | None = "0007_fixed_endzone_regime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("track_points", sa.Column("shot_id", sa.String(120), nullable=True))
    op.add_column("track_points", sa.Column("visibility_state", sa.String(30), nullable=True))
    op.drop_index("uq_players_jersey_posgroup_active", table_name="players")
    op.create_index(
        "ix_players_jersey_posgroup_active",
        "players",
        ["jersey_number", "position_group"],
        postgresql_where=sa.text(
            "is_active AND jersey_number IS NOT NULL AND position_group IS NOT NULL"
        ),
    )
    op.create_table(
        "saved_views",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("team_scope", sa.String(120), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("surface", sa.String(30), nullable=False),
        sa.Column("visibility", sa.String(10), nullable=False),
        sa.Column("filters", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_saved_views_owner_id", "saved_views", ["owner_id"])
    op.create_table(
        "roster_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "player_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("players.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("team", sa.String(120), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(30), nullable=False),
        sa.Column("jersey_number", sa.Integer(), nullable=True),
        sa.Column("position", sa.String(50), nullable=True),
        sa.Column("provenance", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "player_id", "team", "season", "unit", name="uq_roster_assignment_scope"
        ),
    )
    for column in ("player_id", "team", "season"):
        op.create_index(f"ix_roster_assignments_{column}", "roster_assignments", [column])
    # Preserve ambiguous numbers as independent candidates; historical season
    # is not guessed from the migration date. Only explicit roster years seed.
    op.execute("""
        INSERT INTO roster_assignments (id, player_id, team, season, unit,
                                       jersey_number, position, provenance)
        SELECT gen_random_uuid(), id, COALESCE(metadata->>'team', 'Toledo'),
               (metadata->>'season')::integer, 'unknown', jersey_number, position,
               '{"source":"legacy_roster","season_source":"explicit_metadata"}'::jsonb
        FROM players WHERE metadata->>'season' ~ '^[0-9]{4}$'
    """)


def downgrade() -> None:
    # Never erase a legitimate athlete/number merely to restore old uniqueness.
    # Refuse an unsafe downgrade explicitly if this release retained duplicates.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM players WHERE is_active AND jersey_number IS NOT NULL
                       AND position_group IS NOT NULL
                       GROUP BY jersey_number, position_group HAVING count(*) > 1)
            THEN RAISE EXCEPTION 'Resolve duplicate roster candidates before downgrading';
            END IF;
        END $$;
    """)
    op.drop_table("roster_assignments")
    op.drop_column("track_points", "visibility_state")
    op.drop_column("track_points", "shot_id")
    op.drop_table("saved_views")
    op.drop_index("ix_players_jersey_posgroup_active", table_name="players")
    op.create_index(
        "uq_players_jersey_posgroup_active",
        "players",
        ["jersey_number", "position_group"],
        unique=True,
        postgresql_where=sa.text(
            "is_active AND jersey_number IS NOT NULL AND position_group IS NOT NULL"
        ),
    )
