"""ORM models for the CFBD Postgres cache (Issue #162).

These tables cache College Football Data so Toledo/MAC analytics can be built
without a live vendor call on every request. They preserve CFBD source IDs,
carry season/week/season-type/team/opponent/conference/venue/home-away context
where applicable, and never store the vendor API key.

``app.models`` imports this module so the tables register with
``Base.metadata`` and are visible to Alembic and the test suite.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CFBDSyncStatus(enum.StrEnum):
    """Lifecycle status of a single CFBD sync attempt.

    ``ok``/``error`` are backward-compatible aliases for the read-only CFBD
    analytics router/tests that shipped on a parallel branch before the shared
    ingestion package merged to ``main``.
    """

    running = "running"
    succeeded = "succeeded"
    ok = "succeeded"
    failed = "failed"
    error = "failed"
    partial = "partial"


def _uuid_pk() -> Mapped[uuid.UUID]:
    # Server-side default so Core bulk upserts (``INSERT ... VALUES`` with a
    # list of dicts, which skip Python-side column defaults) still get a PK.
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CFBDTeam(Base):
    __tablename__ = "cfbd_teams"
    __table_args__ = (UniqueConstraint("cfbd_team_id", name="uq_cfbd_teams_source_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    cfbd_team_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    school: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    mascot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    abbreviation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conference: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    division: Mapped[str | None] = mapped_column(String(120), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    alt_color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logos: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CFBDGame(Base):
    __tablename__ = "cfbd_games"
    __table_args__ = (UniqueConstraint("cfbd_game_id", name="uq_cfbd_games_source_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    cfbd_game_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    season_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    neutral_site: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    conference_game: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    venue: Mapped[str | None] = mapped_column(String(200), nullable=True)
    venue_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    home_team: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    home_conference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    home_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    away_team: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    away_conference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    away_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CFBDDrive(Base):
    __tablename__ = "cfbd_drives"
    __table_args__ = (UniqueConstraint("cfbd_drive_id", name="uq_cfbd_drives_source_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    cfbd_drive_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    cfbd_game_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    offense: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    offense_conference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    defense: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    defense_conference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    drive_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scoring: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    start_period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plays: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yards: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drive_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CFBDPlay(Base):
    __tablename__ = "cfbd_plays"
    __table_args__ = (UniqueConstraint("cfbd_play_id", name="uq_cfbd_plays_source_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    cfbd_play_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    cfbd_game_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    cfbd_drive_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    season_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    offense: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    offense_conference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    defense: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    defense_conference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    home: Mapped[str | None] = mapped_column(String(120), nullable=True)
    away: Mapped[str | None] = mapped_column(String(120), nullable=True)
    period: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yard_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    down: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yards_gained: Mapped[int | None] = mapped_column(Integer, nullable=True)
    play_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    play_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ppa: Mapped[float | None] = mapped_column(Float, nullable=True)
    scoring: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CFBDTeamGameStat(Base):
    __tablename__ = "cfbd_team_game_stats"
    __table_args__ = (
        UniqueConstraint("cfbd_game_id", "team", name="uq_cfbd_team_game_stats_game_team"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    cfbd_game_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    team: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    team_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    conference: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    opponent: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    home_away: Mapped[str | None] = mapped_column(String(8), nullable=True)
    points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CFBDWinProbability(Base):
    __tablename__ = "cfbd_win_probability"
    __table_args__ = (
        UniqueConstraint("cfbd_game_id", "play_number", name="uq_cfbd_win_probability_game_play"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    cfbd_game_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    play_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    play_number: Mapped[int] = mapped_column(Integer, nullable=False)
    home: Mapped[str | None] = mapped_column(String(120), nullable=True)
    away: Mapped[str | None] = mapped_column(String(120), nullable=True)
    home_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    away_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    home_ball: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yard_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    down: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_win_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    play_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    @property
    def cfbd_play_id(self) -> int | None:
        return self.play_id

    @property
    def home_team(self) -> str | None:
        return self.home

    @property
    def away_team(self) -> str | None:
        return self.away


class CFBDSyncRun(Base):
    """Audit row for every CFBD sync attempt (endpoint, params, counts, status)."""

    __tablename__ = "cfbd_sync_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    season_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[CFBDSyncStatus] = mapped_column(
        Enum(CFBDSyncStatus, name="cfbd_sync_status"),
        nullable=False,
        default=CFBDSyncStatus.running,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
