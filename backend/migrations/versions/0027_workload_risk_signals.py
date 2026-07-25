"""Issue #149 — workload-fusion injury-risk signals.

Adds the three scalar metric columns (sprint_count, asymmetry_index,
injury_risk_score), the ``player_workload_daily`` nightly per-player rollup
table, and the ``workload_risk`` alert type.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("metrics", sa.Column("sprint_count", sa.Integer(), nullable=True))
    op.add_column("metrics", sa.Column("asymmetry_index", sa.Float(), nullable=True))
    op.add_column("metrics", sa.Column("injury_risk_score", sa.Float(), nullable=True))

    op.create_table(
        "player_workload_daily",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "player_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("players.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column("daily_load", sa.Float(), nullable=True),
        sa.Column("sprint_count", sa.Integer(), nullable=True),
        sa.Column("max_speed_yps", sa.Float(), nullable=True),
        sa.Column("asymmetry_index", sa.Float(), nullable=True),
        sa.Column("acute_load_7d", sa.Float(), nullable=True),
        sa.Column("chronic_load_28d", sa.Float(), nullable=True),
        sa.Column("acwr", sa.Float(), nullable=True),
        sa.Column("injury_risk_score", sa.Float(), nullable=True),
        sa.Column("risk_reason_codes", sa.JSON(), nullable=True),
        sa.Column("clip_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attribution", sa.String(length=20), nullable=False, server_default="player"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("model_variant", sa.String(length=50), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("player_id", "date", name="player_workload_daily_unique"),
    )

    # New enum value for workload-risk alerts (AC > 1.5 / asymmetry > 1.3).
    # Postgres requires ADD VALUE to run outside the migration transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alert_type ADD VALUE IF NOT EXISTS 'workload_risk'")


def downgrade() -> None:
    # NOTE: Postgres cannot drop a value from an enum type; the
    # 'workload_risk' alert_type value intentionally survives downgrade.
    op.drop_table("player_workload_daily")
    op.drop_column("metrics", "injury_risk_score")
    op.drop_column("metrics", "asymmetry_index")
    op.drop_column("metrics", "sprint_count")
