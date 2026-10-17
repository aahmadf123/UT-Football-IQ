"""Add the end-zone camera angle of edited game film as a capture regime."""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_fixed_endzone_regime"
down_revision: str | None = "0006_cfbd_player_seasons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block that later uses it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE capture_regime ADD VALUE IF NOT EXISTS 'fixed_endzone'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; move rows back to the generic regime.
    op.execute(
        "UPDATE videos SET capture_regime = 'unconstrained' WHERE capture_regime = 'fixed_endzone'"
    )
    op.execute(
        "UPDATE clips SET capture_regime = 'unconstrained' WHERE capture_regime = 'fixed_endzone'"
    )
