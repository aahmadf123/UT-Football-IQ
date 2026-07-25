"""Issue #195 — raw CLIP image embedding for genuine text-tower search.

Adds ``playembeddings.clip_vector`` — the raw 512-d CLIP ViT-B/32 image
embedding in CLIP's shared text–image space. The existing fused
``vector`` column is **not** in CLIP space (its visual half is a
random-init projection per ``docs/embeddings-architecture.md`` §7), so a
raw CLIP text-tower query cannot be cosine-compared against it. Storing
the unprojected CLIP embedding lets ``POST /api/v1/search/text`` match a
text query directly against the image embedding (Issue #195).

The column is nullable: it is populated only when a real CLIP visual
encoder ran (the default ``ZeroVisualEncoder`` leaves it NULL) and is
backfilled incrementally by the nightly embed stage — no second vector
store is introduced (reuses pgvector / ``playembeddings``).

A cosine ``ivfflat`` index mirrors the existing ``vector`` index so the
text-search ORDER BY is index-served.

This migration is guarded on pgvector availability exactly like
``0008_play_embeddings`` — on a Postgres instance without the ``vector``
extension, ``0008`` skips creating ``playembeddings`` entirely, so this
migration must skip too (the table would not exist). That keeps
``alembic upgrade head`` / ``downgrade base`` clean on either kind of
instance.

Revision ID: 0022
Revises: 0021_clip_result_state
Create Date: 2026-05-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021_clip_result_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PLAY_EMBEDDING_CLIP_DIM = 512


def _pgvector_available() -> bool:
    """Whether the ``vector`` extension exists on this instance.

    Matches the guard in ``0008_play_embeddings``: when pgvector is
    unavailable that migration is a no-op (no ``playembeddings`` table),
    so this one must be a no-op as well.
    """
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        ).scalar()
    )


def upgrade() -> None:
    if not _pgvector_available():
        return  # pgvector unavailable — 0008 skipped, so playembeddings does not exist
    # ``vector`` user type DDL must be raw SQL — Alembic core does not know it.
    op.execute(
        f"ALTER TABLE playembeddings ADD COLUMN clip_vector vector({PLAY_EMBEDDING_CLIP_DIM})"
    )
    # Cosine ivfflat index, mirroring playembeddings_vector_ivfflat. ``lists=100``
    # is the same season-scale default; swap to hnsw with a single ALTER once the
    # populated row count justifies it.
    op.execute(
        "CREATE INDEX playembeddings_clip_vector_ivfflat "
        "ON playembeddings USING ivfflat (clip_vector vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    if not _pgvector_available():
        return
    op.execute("DROP INDEX IF EXISTS playembeddings_clip_vector_ivfflat")
    op.execute("ALTER TABLE playembeddings DROP COLUMN IF EXISTS clip_vector")
