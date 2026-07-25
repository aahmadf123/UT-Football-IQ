"""Issue #8 — Phase 3: learned play embeddings + zero-shot concept review.

Adds:
  - ``vector`` Postgres extension (pgvector).
  - ``playembeddings`` table — one row per (clip, chunk, model version) with
    a fused 256-d vector plus retained 192-d/64-d sub-embeddings and full
    lineage back to the labels/clip/model version that produced it.
  - ``embedding_cluster_proposals`` table — coach review queue for
    HDBSCAN-derived experimental concept clusters.
  - ``embeddings`` value on the ``job_type`` enum so the dispatcher can
    queue a nightly ``stage_embed`` run.

Vector indexes use ``ivfflat`` with cosine distance (per
``docs/embeddings-architecture.md`` §8); a swap to ``hnsw`` is a single
ALTER once the row count justifies it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLAY_EMBEDDING_DIM = 256
PLAY_EMBEDDING_VISUAL_DIM = 192
PLAY_EMBEDDING_STRUCTURED_DIM = 64


def upgrade() -> None:
    # ── pgvector ──────────────────────────────────────────────────────────────
    # Skip silently if pgvector is not installed on this Postgres instance.
    conn = op.get_bind()
    has_vector = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).scalar()
    if not has_vector:
        return  # pgvector unavailable — skip entire migration; re-run after enabling it
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── Expand job_type enum so the dispatcher can queue an embed job ────────
    op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'embeddings'")

    # ── playembeddings ────────────────────────────────────────────────────────
    op.create_table(
        "playembeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "clip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_kind", sa.String(20), nullable=False, server_default="play"),
        sa.Column("snap_anchor", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "model_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "calibration_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("field_calibrations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_label_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("used_sam_masks", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("embedding_confidence", sa.Float(), nullable=True),
        sa.Column(
            "is_experimental",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "clip_id",
            "chunk_kind",
            "model_version_id",
            name="uq_playembeddings_clip_chunk_modelversion",
        ),
    )
    # vector / pgvector columns must be added with raw SQL — Alembic's
    # core type system doesn't know about the ``vector`` user type.
    op.execute(
        f"ALTER TABLE playembeddings ADD COLUMN vector vector({PLAY_EMBEDDING_DIM}) NOT NULL"
    )
    op.execute(
        f"ALTER TABLE playembeddings ADD COLUMN visual_vector vector({PLAY_EMBEDDING_VISUAL_DIM})"
    )
    op.execute(
        f"ALTER TABLE playembeddings "
        f"ADD COLUMN structured_vector vector({PLAY_EMBEDDING_STRUCTURED_DIM})"
    )

    op.create_index("playembeddings_clip_id_idx", "playembeddings", ["clip_id"], unique=False)
    op.create_index(
        "playembeddings_model_version_id_idx",
        "playembeddings",
        ["model_version_id"],
        unique=False,
    )
    # ivfflat cosine index for top-K similarity. ``lists=100`` is a sensible
    # default for the season-scale row count; swap to hnsw with a single
    # ALTER once recall degrades or rows exceed ~50k.
    op.execute(
        "CREATE INDEX playembeddings_vector_ivfflat "
        "ON playembeddings USING ivfflat (vector vector_cosine_ops) WITH (lists = 100)"
    )

    # ── embedding_cluster_proposals ───────────────────────────────────────────
    op.create_table(
        "embedding_cluster_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "model_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("model_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cluster_label", sa.String(64), nullable=False),
        sa.Column("proposed_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "member_clip_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cohesion_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "is_experimental",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("accepted_label_name", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        f"ALTER TABLE embedding_cluster_proposals ADD COLUMN centroid vector({PLAY_EMBEDDING_DIM})"
    )
    op.create_index(
        "embedding_cluster_proposals_status_idx",
        "embedding_cluster_proposals",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS embedding_cluster_proposals_status_idx")
    op.execute("DROP TABLE IF EXISTS embedding_cluster_proposals")
    op.execute("DROP INDEX IF EXISTS playembeddings_vector_ivfflat")
    op.execute("DROP INDEX IF EXISTS playembeddings_model_version_id_idx")
    op.execute("DROP INDEX IF EXISTS playembeddings_clip_id_idx")
    op.execute("DROP TABLE IF EXISTS playembeddings")
    # NOTE: we intentionally do not remove ``embeddings`` from the job_type
    # enum. Postgres ALTER TYPE ... DROP VALUE has no first-class support
    # and the value is safe to leave behind even if no rows reference it.
