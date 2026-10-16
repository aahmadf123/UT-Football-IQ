"""Cache CFBD per-player season stats, usage and PPA for player profiles."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_cfbd_player_seasons"
down_revision: str | None = "0005_recording_content_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cfbd_player_seasons",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("athlete_id", sa.BigInteger(), nullable=False),
        sa.Column("team", sa.String(120), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("position", sa.String(32), nullable=True),
        sa.Column("stats", postgresql.JSONB(), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("ppa", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("season", "athlete_id", name="uq_cfbd_player_seasons_season_athlete"),
    )
    op.create_index("ix_cfbd_player_seasons_season", "cfbd_player_seasons", ["season"])
    op.create_index("ix_cfbd_player_seasons_athlete_id", "cfbd_player_seasons", ["athlete_id"])
    # Roster sync looks players up by their CFBD athlete id inside metadata.
    op.create_index(
        "ix_players_cfbd_athlete_id",
        "players",
        [sa.text("(metadata ->> 'cfbd_athlete_id')")],
    )


def downgrade() -> None:
    op.drop_index("ix_players_cfbd_athlete_id", table_name="players")
    op.drop_index("ix_cfbd_player_seasons_athlete_id", table_name="cfbd_player_seasons")
    op.drop_index("ix_cfbd_player_seasons_season", table_name="cfbd_player_seasons")
    op.drop_table("cfbd_player_seasons")
