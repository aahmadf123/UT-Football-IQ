"""Prevent duplicate shared recordings using server-verified content identity."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_recording_content_identity"
down_revision: str | None = "0004_track_point_timestamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Historical rows stay NULL: migrations must not download and hash a film
    # library, nor delete older recordings that may already have annotations.
    op.add_column("videos", sa.Column("content_sha256", sa.String(64), nullable=True))
    op.add_column("videos", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.create_unique_constraint("videos_content_sha256_key", "videos", ["content_sha256"])


def downgrade() -> None:
    op.drop_constraint("videos_content_sha256_key", "videos", type_="unique")
    op.drop_column("videos", "size_bytes")
    op.drop_column("videos", "content_sha256")
