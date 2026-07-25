"""Issue #9 — restricted-field policy flags on player profiles.

Adds ``player_profiles.restricted_context_flags``: per-player JSON overrides
of the canonical health-context field→tier mapping (workload / rehab /
medical). Overrides may only escalate a field to a stricter tier — they can
never widen access (enforced in ``app.governance``).

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "player_profiles",
        sa.Column("restricted_context_flags", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_profiles", "restricted_context_flags")
