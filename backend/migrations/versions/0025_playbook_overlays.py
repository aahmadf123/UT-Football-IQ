"""Playbook overlays & assignment execution scoring (Issue #15).

Adds three light, coach-edited tables that turn the call sheet into a film-room
overlay layer:

* ``playbook_concepts`` — coach-authored concepts (offense/defense) plus their
  assignment definitions (intended routes, coaching points, scoring rules).
* ``play_concepts`` — links a concept to the clip (play) it was called on, with
  the assignment→tracklet map and a coach-validated flag.
* ``assignment_scores`` — per-assignment rule-based execution grades with
  confidence/uncertainty, reason codes, and a coach-override surface.

No new vector store and no enum types — grades/sides are plain strings so the
migration stays additive. See ``docs/playbook-overlays.md``.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-01 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playbook_concepts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("structure_kind", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("assignments", sa.JSON(), nullable=False),
        sa.Column("coaching_points", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("name", name="playbook_concepts_name_uq"),
    )
    op.create_index("ix_playbook_concepts_name", "playbook_concepts", ["name"])

    op.create_table(
        "play_concepts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "clip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "concept_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("playbook_concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default="coach"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("assignment_player_map", sa.JSON(), nullable=True),
        sa.Column("validated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("clip_id", "concept_id", name="play_concepts_clip_concept_uq"),
    )
    op.create_index("ix_play_concepts_clip_id", "play_concepts", ["clip_id"])
    op.create_index("ix_play_concepts_concept_id", "play_concepts", ["concept_id"])

    op.create_table(
        "assignment_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "clip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "concept_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("playbook_concepts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assignment_key", sa.String(64), nullable=False),
        sa.Column("grade", sa.String(24), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("uncertainty", sa.Float(), nullable=False, server_default="1"),
        sa.Column("reason_codes", sa.JSON(), nullable=True),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("experimental", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(24), nullable=False, server_default="auto"),
        sa.Column("coach_grade", sa.String(24), nullable=True),
        sa.Column("coach_comment", sa.Text(), nullable=True),
        sa.Column(
            "overridden_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "clip_id", "concept_id", "assignment_key", name="assignment_scores_unique"
        ),
    )
    op.create_index("ix_assignment_scores_clip_id", "assignment_scores", ["clip_id"])
    op.create_index("ix_assignment_scores_concept_id", "assignment_scores", ["concept_id"])


def downgrade() -> None:
    op.drop_index("ix_assignment_scores_concept_id", table_name="assignment_scores")
    op.drop_index("ix_assignment_scores_clip_id", table_name="assignment_scores")
    op.drop_table("assignment_scores")
    op.drop_index("ix_play_concepts_concept_id", table_name="play_concepts")
    op.drop_index("ix_play_concepts_clip_id", table_name="play_concepts")
    op.drop_table("play_concepts")
    op.drop_index("ix_playbook_concepts_name", table_name="playbook_concepts")
    op.drop_table("playbook_concepts")
