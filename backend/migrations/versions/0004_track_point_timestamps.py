"""Preserve presentation timestamps for variable-frame-rate film overlays."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_track_point_timestamps"
down_revision: str | None = "0003_local_sessions_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("track_points", sa.Column("timestamp_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("track_points", "timestamp_seconds")
