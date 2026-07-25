"""Player development passport tables (Issue #7).

Adds the longitudinal player development passport:

* ``player_profiles`` — one row per ``(player, season)``: role summary,
  development goals, private coach notes, player-facing summary, benchmark
  group, curated clips, and authoring/approval bookkeeping.
* ``player_profile_snapshots`` — weekly snapshots: summary metrics, strengths,
  development focus, evidence clips, a confidence-scored identity resolution,
  and an approval lifecycle.

Outward-facing visibility stays on ``players.visibility_state`` (Issue #114);
these tables do not duplicate it. See ``docs/governance.md``.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-31 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


GENERATION_SOURCE = "profile_generation_source"
GENERATION_SOURCE_VALUES = ("manual", "model_assisted", "imported")

IDENTITY_STATE = "player_identity_state"
IDENTITY_STATE_VALUES = ("known", "probable", "unknown", "conflicting", "needs_review")

APPROVAL_STATUS = "snapshot_approval_status"
APPROVAL_STATUS_VALUES = ("pending", "approved", "rejected")


def upgrade() -> None:
    conn = op.get_bind()

    # Create enum types up-front (idempotent), then reference them with
    # ``create_type=False`` so ``create_table`` does not re-issue CREATE TYPE.
    postgresql.ENUM(*GENERATION_SOURCE_VALUES, name=GENERATION_SOURCE).create(conn, checkfirst=True)
    postgresql.ENUM(*IDENTITY_STATE_VALUES, name=IDENTITY_STATE).create(conn, checkfirst=True)
    postgresql.ENUM(*APPROVAL_STATUS_VALUES, name=APPROVAL_STATUS).create(conn, checkfirst=True)

    generation_enum = postgresql.ENUM(
        *GENERATION_SOURCE_VALUES, name=GENERATION_SOURCE, create_type=False
    )
    identity_enum = postgresql.ENUM(*IDENTITY_STATE_VALUES, name=IDENTITY_STATE, create_type=False)
    approval_enum = postgresql.ENUM(
        *APPROVAL_STATUS_VALUES, name=APPROVAL_STATUS, create_type=False
    )

    op.create_table(
        "player_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "player_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("players.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("season", sa.String(16), nullable=False),
        sa.Column("role_summary", sa.Text(), nullable=True),
        sa.Column("development_goals", sa.JSON(), nullable=True),
        sa.Column("coach_notes", sa.Text(), nullable=True),
        sa.Column("player_summary", sa.Text(), nullable=True),
        sa.Column("benchmark_group", sa.String(40), nullable=True),
        sa.Column("favorite_clip_ids", sa.JSON(), nullable=True),
        sa.Column(
            "generated_by",
            generation_enum,
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("player_id", "season", name="player_profiles_player_season_uq"),
    )
    op.create_index("ix_player_profiles_player_id", "player_profiles", ["player_id"])

    op.create_table(
        "player_profile_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("player_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "player_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("players.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("summary_metrics", sa.JSON(), nullable=True),
        sa.Column("strengths", sa.JSON(), nullable=True),
        sa.Column("development_focus", sa.JSON(), nullable=True),
        sa.Column("evidence_clip_ids", sa.JSON(), nullable=True),
        sa.Column(
            "identity_state",
            identity_enum,
            nullable=False,
            server_default="needs_review",
        ),
        sa.Column("identity_confidence", sa.Float(), nullable=True),
        sa.Column("identity_signals", sa.JSON(), nullable=True),
        sa.Column(
            "generated_by",
            generation_enum,
            nullable=False,
            server_default="model_assisted",
        ),
        sa.Column(
            "approval_status",
            approval_enum,
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "experimental_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_player_profile_snapshots_profile_id", "player_profile_snapshots", ["profile_id"]
    )
    op.create_index(
        "ix_player_profile_snapshots_player_id", "player_profile_snapshots", ["player_id"]
    )
    op.create_index(
        "ix_player_profile_snapshots_week_start", "player_profile_snapshots", ["week_start"]
    )


def downgrade() -> None:
    op.drop_index("ix_player_profile_snapshots_week_start", table_name="player_profile_snapshots")
    op.drop_index("ix_player_profile_snapshots_player_id", table_name="player_profile_snapshots")
    op.drop_index("ix_player_profile_snapshots_profile_id", table_name="player_profile_snapshots")
    op.drop_table("player_profile_snapshots")
    op.drop_index("ix_player_profiles_player_id", table_name="player_profiles")
    op.drop_table("player_profiles")

    postgresql.ENUM(name=APPROVAL_STATUS).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name=IDENTITY_STATE).drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name=GENERATION_SOURCE).drop(op.get_bind(), checkfirst=True)
