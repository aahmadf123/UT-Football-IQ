"""Issue #9 — health data-fusion tables.

Creates the five UT-source join tables for the athlete health/workload
dashboard: wellness self-reports, GPS/wearable daily loads (Catapult-style),
strength & conditioning sessions, the team-level academic calendar overlay,
and coarse injury history (rehab-tier restricted; no diagnosis free-text by
design). Columns deliberately match the placeholder IntegrationContract
data_categories shipped by Issue #113.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _player_fk() -> sa.Column:
    return sa.Column(
        "player_id",
        sa.UUID(as_uuid=True),
        sa.ForeignKey("players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def _provenance_columns() -> list[sa.Column]:
    return [
        sa.Column("source", sa.String(length=50), nullable=False, server_default="manual_csv"),
        sa.Column(
            "created_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "wellness_entries",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        _player_fk(),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column("soreness", sa.SmallInteger(), nullable=True),
        sa.Column("sleep_hours", sa.Float(), nullable=True),
        sa.Column("sleep_quality", sa.SmallInteger(), nullable=True),
        sa.Column("energy", sa.SmallInteger(), nullable=True),
        sa.Column("stress", sa.SmallInteger(), nullable=True),
        *_provenance_columns(),
        sa.UniqueConstraint("player_id", "date", name="wellness_entries_unique"),
    )

    op.create_table(
        "gps_workload_daily",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        _player_fk(),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column("session_type", sa.String(length=30), nullable=False, server_default="practice"),
        sa.Column("total_distance_m", sa.Float(), nullable=True),
        sa.Column("high_speed_distance_m", sa.Float(), nullable=True),
        sa.Column("sprint_count", sa.Integer(), nullable=True),
        sa.Column("max_speed_ms", sa.Float(), nullable=True),
        sa.Column("accel_count", sa.Integer(), nullable=True),
        sa.Column("decel_count", sa.Integer(), nullable=True),
        sa.Column("player_load", sa.Float(), nullable=True),
        *_provenance_columns(),
        sa.UniqueConstraint("player_id", "date", "session_type", name="gps_workload_daily_unique"),
    )

    op.create_table(
        "sc_sessions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        _player_fk(),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column("session_volume", sa.Float(), nullable=True),
        sa.Column("tonnage_kg", sa.Float(), nullable=True),
        sa.Column("session_rpe", sa.Float(), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        *_provenance_columns(),
        sa.UniqueConstraint("player_id", "date", name="sc_sessions_unique"),
    )

    op.create_table(
        "academic_calendar_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False, index=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        *_provenance_columns(),
    )

    op.create_table(
        "injury_history",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        _player_fk(),
        sa.Column("reported_date", sa.Date(), nullable=False, index=True),
        sa.Column("body_region", sa.String(length=50), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("days_missed", sa.Integer(), nullable=True),
        sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_provenance_columns(),
    )


def downgrade() -> None:
    op.drop_table("injury_history")
    op.drop_table("academic_calendar_events")
    op.drop_table("sc_sessions")
    op.drop_table("gps_workload_daily")
    op.drop_table("wellness_entries")
