"""Practice sessions router — Hudl-style session grouping for the library.

Returns one card per logical session. A session is keyed by:

* ``practice_session_id`` when the video carries one, or
* ``(recorded_at::date, session_kind, opponent_team)`` otherwise.

This is the minimal data contract needed for the future library UI (#100).
It is not the full library endpoint — clip counts, thumbnails, and
session lifecycle (open / under review / archived) land later.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import SessionKind, User, Video

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/practice-sessions", tags=["practice-sessions"])


class PracticeSessionGroup(BaseModel):
    practice_session_id: uuid.UUID | None
    session_date: date | None
    session_kind: SessionKind | None
    opponent_team: str | None
    video_count: int
    first_recorded_at: datetime | None
    last_recorded_at: datetime | None


@router.get("", response_model=list[PracticeSessionGroup])
async def list_practice_sessions(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
    recorded_after: datetime | None = Query(default=None),
    recorded_before: datetime | None = Query(default=None),
    session_kind: SessionKind | None = Query(default=None),
    opponent_team: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PracticeSessionGroup]:
    """Group videos by ``practice_session_id`` (when present) or by
    ``(recorded_at::date, session_kind, opponent_team)`` otherwise.

    Returns most-recent-session-first based on the latest video's
    ``recorded_at`` within the group.
    """
    fallback_session_date = case(
        (Video.practice_session_id.is_(None), func.date_trunc("day", Video.recorded_at)),
        else_=None,
    )
    fallback_session_kind = case(
        (Video.practice_session_id.is_(None), Video.session_kind),
        else_=None,
    )
    fallback_opponent_team = case(
        (Video.practice_session_id.is_(None), Video.opponent_team),
        else_=None,
    )
    session_date = func.date_trunc("day", func.min(Video.recorded_at)).label("session_date")
    last_seen = func.max(Video.recorded_at)

    q = (
        select(
            Video.practice_session_id.label("practice_session_id"),
            session_date,
            func.max(Video.session_kind).label("session_kind"),
            func.max(Video.opponent_team).label("opponent_team"),
            func.count(Video.id).label("video_count"),
            func.min(Video.recorded_at).label("first_recorded_at"),
            last_seen.label("last_recorded_at"),
        )
        .group_by(
            Video.practice_session_id,
            fallback_session_date,
            fallback_session_kind,
            fallback_opponent_team,
        )
        .order_by(last_seen.desc().nullslast())
        .limit(limit)
    )
    if recorded_after is not None:
        q = q.where(Video.recorded_at >= recorded_after)
    if recorded_before is not None:
        q = q.where(Video.recorded_at <= recorded_before)
    if session_kind is not None:
        q = q.where(Video.session_kind == session_kind)
    if opponent_team is not None:
        q = q.where(Video.opponent_team == opponent_team)

    result = await db.execute(q)
    out: list[PracticeSessionGroup] = []
    for row in result.mappings():
        sd = row["session_date"]
        out.append(
            PracticeSessionGroup(
                practice_session_id=row["practice_session_id"],
                session_date=sd.date() if isinstance(sd, datetime) else sd,
                session_kind=row["session_kind"],
                opponent_team=row["opponent_team"],
                video_count=int(row["video_count"]),
                first_recorded_at=row["first_recorded_at"],
                last_recorded_at=row["last_recorded_at"],
            )
        )
    return out
