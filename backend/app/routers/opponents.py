"""Opponents router — derive the opponent list from game film (Issue #105).

The Opponent Scout UI needs a server-backed picker that lists opponents and the
specific opponent film a coach can scout. Today opponents are not their own
domain table — they are inferred from videos that have ``session_kind=game``
and a non-null ``opponent_team`` field. This router exposes that derived view
so the frontend does not need to scan the entire video list to build a picker.

Endpoints:
    GET /api/v1/opponents              — list opponents with associated film
    GET /api/v1/opponents/{opponent}   — single opponent with its videos
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import SessionKind, User, Video, VideoStatus

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/opponents", tags=["opponents"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class OpponentVideo(BaseModel):
    video_id: uuid.UUID
    filename: str
    status: VideoStatus
    recorded_at: datetime | None
    created_at: str


class OpponentSummary(BaseModel):
    opponent_team: str
    video_count: int
    latest_recorded_at: datetime | None
    videos: list[OpponentVideo]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[OpponentSummary])
async def list_opponents(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> list[OpponentSummary]:
    """List opponents derived from ``session_kind=game`` videos.

    Opponents are sorted by their most recent film first so the UI surfaces
    upcoming/recent matchups before older ones. Each entry includes the video
    list the Opponent Scout picker uses to drive ``opponent_video_id``.
    """
    result = await db.execute(
        select(Video)
        .where(Video.session_kind == SessionKind.game)
        .where(Video.opponent_team.is_not(None))
        .order_by(Video.recorded_at.desc().nullslast(), Video.created_at.desc())
    )
    videos = list(result.scalars().all())

    grouped: dict[str, list[Video]] = {}
    for v in videos:
        # opponent_team is non-null because of the WHERE clause above.
        team = (v.opponent_team or "").strip()
        if not team:
            continue
        grouped.setdefault(team, []).append(v)

    summaries: list[OpponentSummary] = []
    for team, team_videos in grouped.items():
        latest = max(
            (v.recorded_at for v in team_videos if v.recorded_at is not None),
            default=None,
        )
        summaries.append(
            OpponentSummary(
                opponent_team=team,
                video_count=len(team_videos),
                latest_recorded_at=latest,
                videos=[
                    OpponentVideo(
                        video_id=v.id,
                        filename=v.filename,
                        status=v.status,
                        recorded_at=v.recorded_at,
                        created_at=v.created_at.isoformat(),
                    )
                    for v in team_videos
                ],
            )
        )

    summaries.sort(
        key=lambda s: (
            s.latest_recorded_at is None,
            0.0 if s.latest_recorded_at is None else -s.latest_recorded_at.timestamp(),
        ),
    )
    return summaries


@router.get("/{opponent_team}", response_model=OpponentSummary)
async def get_opponent(
    opponent_team: str,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> OpponentSummary:
    """Return a single opponent and the game film tagged to them."""
    cleaned = opponent_team.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="opponent_team is required",
        )
    result = await db.execute(
        select(Video)
        .where(Video.session_kind == SessionKind.game)
        .where(Video.opponent_team == cleaned)
        .order_by(Video.recorded_at.desc().nullslast(), Video.created_at.desc())
    )
    team_videos = list(result.scalars().all())
    if not team_videos:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No game film for opponent '{cleaned}'",
        )
    latest = max(
        (v.recorded_at for v in team_videos if v.recorded_at is not None),
        default=None,
    )
    return OpponentSummary(
        opponent_team=cleaned,
        video_count=len(team_videos),
        latest_recorded_at=latest,
        videos=[
            OpponentVideo(
                video_id=v.id,
                filename=v.filename,
                status=v.status,
                recorded_at=v.recorded_at,
                created_at=v.created_at.isoformat(),
            )
            for v in team_videos
        ],
    )
