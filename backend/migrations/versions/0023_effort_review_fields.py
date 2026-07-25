"""Issue #142 — effort review scalar fields on metrics.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("metrics", sa.Column("effort_zscore", sa.Float(), nullable=True))
    op.add_column("metrics", sa.Column("loaf_flag", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("metrics", "loaf_flag")
    op.drop_column("metrics", "effort_zscore")
