"""Any-camera posture: add the ``unconstrained`` capture regime (ADR 0005).

Analyzed footage that matches neither special regime (drone_follow /
fixed_sideline) is now first-class ``unconstrained`` — endzone, handheld,
tripod, phone, anything — and takes the generic pipeline path with
recall-first detection. ``unknown`` narrows to hard analysis failures.

Enum-only change; runs in an autocommit block for the same reason as 0030.

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-24 02:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE capture_regime ADD VALUE IF NOT EXISTS 'unconstrained'")


def downgrade() -> None:
    # Postgres cannot drop enum values; the inert value is harmless.
    pass
