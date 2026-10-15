"""Persist sessions, independent play views, worker capabilities, and durable leases.

Existing video/clip identities are retained. This migration is run only by the
installer against its selected database; no application startup auto-migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0003_local_sessions_runtime"
down_revision: str | None = "0002_roster_uniqueness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'paused'")
    op.create_table(
        "football_sessions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("session_kind", pg.ENUM(name="session_kind", create_type=False)),
        sa.Column("recorded_at", sa.DateTime(timezone=True)),
        sa.Column("opponent_team", sa.String(120)),
        sa.Column("library_role", sa.String(20), nullable=False, server_default="toledo"),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "created_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Existing grouping UUIDs become actual sessions, including empty-date film.
    op.execute("""INSERT INTO football_sessions(id,title,session_kind,recorded_at,opponent_team)
        SELECT practice_session_id, 'Imported session', min(session_kind::text)::session_kind,
          min(recorded_at), min(opponent_team) FROM videos
        WHERE practice_session_id IS NOT NULL GROUP BY practice_session_id""")
    # Each previously ungrouped recording remains independently usable.
    op.execute("""INSERT INTO football_sessions(id,title,session_kind,recorded_at,opponent_team)
        SELECT id, left(filename,255), session_kind, recorded_at, opponent_team FROM videos
        WHERE practice_session_id IS NULL ON CONFLICT(id) DO NOTHING""")
    op.execute("UPDATE videos SET practice_session_id=id WHERE practice_session_id IS NULL")
    op.create_foreign_key(
        "fk_videos_session",
        "videos",
        "football_sessions",
        ["practice_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "videos", sa.Column("library_role", sa.String(20), nullable=False, server_default="toledo")
    )
    op.add_column("videos", sa.Column("camera_label", sa.String(120)))
    op.create_table(
        "plays",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("football_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255)),
        sa.Column("sequence", sa.Integer()),
        sa.Column("metadata", pg.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_plays_session_id", "plays", ["session_id"])
    op.create_table(
        "play_views",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "play_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("plays.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "clip_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("clips.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("alignment_offset_seconds", sa.Float()),
        sa.Column("alignment_confidence", sa.Float()),
        sa.Column("match_source", sa.String(20), nullable=False, server_default="independent"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_play_views_play_id", "play_views", ["play_id"])
    op.execute(
        "INSERT INTO plays(id,session_id,created_at) "
        "SELECT c.id,v.practice_session_id,c.created_at "
        "FROM clips c JOIN videos v ON v.id=c.video_id"
    )
    op.execute(
        "INSERT INTO play_views(id,play_id,clip_id,match_source) "
        "SELECT id,id,id,'independent' FROM clips"
    )
    op.create_table(
        "worker_runtimes",
        sa.Column("worker_id", sa.String(128), primary_key=True),
        sa.Column("device", sa.String(20), nullable=False, server_default="cpu"),
        sa.Column("capabilities", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "registered_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
    )
    for table in (
        "clips",
        "tracklets",
        "metrics",
        "processing_jobs",
        "field_calibrations",
        "events",
    ):
        op.add_column(table, sa.Column("provenance", pg.JSONB(), nullable=True))
    op.add_column("processing_jobs", sa.Column("lease_token", pg.UUID(as_uuid=True)))
    op.add_column("processing_jobs", sa.Column("control_requested", sa.String(20)))
    op.add_column("report_jobs", sa.Column("lease_token", pg.UUID(as_uuid=True)))
    op.add_column("report_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "report_jobs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    for column in ("lease_token", "lease_expires_at", "attempt_count"):
        op.drop_column("report_jobs", column)
    for column in ("lease_token", "control_requested"):
        op.drop_column("processing_jobs", column)
    for table in (
        "clips",
        "tracklets",
        "metrics",
        "processing_jobs",
        "field_calibrations",
        "events",
    ):
        op.drop_column(table, "provenance")
    op.drop_table("worker_runtimes")
    op.drop_table("play_views")
    op.drop_table("plays")
    op.drop_constraint("fk_videos_session", "videos", type_="foreignkey")
    op.drop_column("videos", "camera_label")
    op.drop_column("videos", "library_role")
    op.drop_table("football_sessions")
    # PostgreSQL enum values are intentionally retained; deleting one can corrupt rows.
