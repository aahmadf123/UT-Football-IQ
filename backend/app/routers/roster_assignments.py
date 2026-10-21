"""Season/team/unit roster candidates; duplicate numbers are retained."""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff, require_coach_or_above
from app.governance import VisibilityMode, shape_player
from app.models import FootballSession, Player, RosterAssignment, SessionKind, User, Video

router = APIRouter(prefix="/api/v1/roster-assignments", tags=["roster-assignments"])


class AssignmentCreate(BaseModel):
    player_id: uuid.UUID
    team: str = Field(default="Toledo", min_length=1, max_length=120)
    season: int = Field(ge=1900, le=2200)
    unit: Literal["offense", "defense", "special_teams", "unknown"] = "unknown"
    jersey_number: int | None = Field(default=None, ge=0, le=99)
    position: str | None = Field(default=None, max_length=50)
    session_id: uuid.UUID | None = None
    effective_from: date | None = None
    effective_until: date | None = None

    @model_validator(mode="after")
    def dates(self) -> "AssignmentCreate":
        if (
            self.effective_from
            and self.effective_until
            and self.effective_from > self.effective_until
        ):
            raise ValueError("Effective start must precede end")
        return self

    @field_validator("team")
    @classmethod
    def clean_team(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Team cannot be blank")
        return value.strip()


def serialize(assignment: RosterAssignment) -> dict[str, Any]:
    return {
        "id": str(assignment.id),
        "player_id": str(assignment.player_id),
        "team": assignment.team,
        "season": assignment.season,
        "unit": assignment.unit,
        "jersey_number": assignment.jersey_number,
        "position": assignment.position,
        "provenance": assignment.provenance,
        "session_id": str(assignment.session_id) if assignment.session_id else None,
        "effective_from": assignment.effective_from.isoformat()
        if assignment.effective_from
        else None,
        "effective_until": assignment.effective_until.isoformat()
        if assignment.effective_until
        else None,
    }


@router.get("")
async def list_assignments(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
    team: str | None = Query(default=None, max_length=120),
    season: int | None = Query(default=None, ge=1900, le=2200),
    jersey_number: int | None = Query(default=None, ge=0, le=99),
) -> list[dict[str, Any]]:
    stmt = select(RosterAssignment)
    if team is not None:
        stmt = stmt.where(RosterAssignment.team == team)
    if season is not None:
        stmt = stmt.where(RosterAssignment.season == season)
    if jersey_number is not None:
        stmt = stmt.where(RosterAssignment.jersey_number == jersey_number)
    return [
        serialize(row)
        for row in (
            await db.execute(
                stmt.order_by(RosterAssignment.season.desc(), RosterAssignment.team).limit(10000)
            )
        )
        .scalars()
        .all()
    ]


@router.post("")
async def upsert_assignment(
    body: AssignmentCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    # The parent lock serializes writes to this player's assignment scopes.
    player = (
        await db.execute(select(Player).where(Player.id == body.player_id).with_for_update())
    ).scalar_one_or_none()
    if player is None:
        raise HTTPException(404, "Player not found")
    if body.session_id and await db.get(FootballSession, body.session_id) is None:
        raise HTTPException(404, "Session not found")
    row = (
        await db.execute(
            select(RosterAssignment).where(
                RosterAssignment.player_id == body.player_id,
                RosterAssignment.team == body.team,
                RosterAssignment.season == body.season,
                RosterAssignment.unit == body.unit,
                RosterAssignment.session_id.is_(None)
                if body.session_id is None
                else RosterAssignment.session_id == body.session_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = RosterAssignment(id=uuid.uuid4(), **body.model_dump())
        db.add(row)
    else:
        row.jersey_number = body.jersey_number
        row.position = body.position
        row.effective_from = body.effective_from
        row.effective_until = body.effective_until
    row.provenance = {"source": "human", "actor_id": str(user.id)}
    await db.flush()
    return serialize(row)


@router.get("/for-video/{video_id}")
async def roster_for_video(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> list[dict[str, Any]]:
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Video not found")
    recorded = video.recorded_at.date() if video.recorded_at else None
    season = video.recorded_at.year if video.recorded_at else (video.metadata_ or {}).get("season")
    if season is not None and (not str(season).isdigit() or not 1900 <= int(season) <= 2200):
        season = None
    if recorded and recorded.month < 3:
        season = recorded.year - 1
    stmt = select(RosterAssignment).where(
        func.lower(RosterAssignment.team) == "toledo",
        or_(
            RosterAssignment.session_id.is_(None),
            RosterAssignment.session_id == video.practice_session_id,
        ),
    )
    if season is not None:
        stmt = stmt.where(RosterAssignment.season == int(season))
    else:
        # An unknown recording season cannot justify a dated/temporary assignment.
        stmt = (
            stmt.where(RosterAssignment.session_id == video.practice_session_id)
            if video.practice_session_id
            else stmt.where(false())
        )
    if recorded:
        stmt = stmt.where(
            or_(
                RosterAssignment.effective_from.is_(None),
                RosterAssignment.effective_from <= recorded,
            ),
            or_(
                RosterAssignment.effective_until.is_(None),
                RosterAssignment.effective_until >= recorded,
            ),
        )
    else:
        stmt = stmt.where(
            RosterAssignment.effective_from.is_(None), RosterAssignment.effective_until.is_(None)
        )
    assignments = (await db.execute(stmt)).scalars().all()
    selected: dict[tuple[uuid.UUID, str], RosterAssignment] = {}
    for assignment in assignments:
        key = (assignment.player_id, assignment.unit)
        if key not in selected or assignment.session_id is not None:
            selected[key] = assignment
    ids = {assignment.player_id for assignment in selected.values()}
    player_scope = or_(Player.is_active.is_(True), Player.id.in_(ids))
    if season is not None:
        player_scope = or_(player_scope, Player.metadata_["roster_season"].astext == str(season))
    players = (await db.execute(select(Player).where(player_scope))).scalars().all()
    assigned_ids = set(
        (
            await db.execute(
                select(RosterAssignment.player_id).where(RosterAssignment.season == int(season))
                if season is not None
                else select(RosterAssignment.player_id)
            )
        )
        .scalars()
        .all()
    )
    by_id = {player.id: player for player in players}
    output = []
    scope = (
        "intrasquad"
        if video.session_kind in (SessionKind.practice, SessionKind.scrimmage)
        else "resolved_team_required"
    )
    for assignment in selected.values():
        player = by_id.get(assignment.player_id)
        shaped = shape_player(player, VisibilityMode.STAFF, actor=user) if player else None
        if shaped and player is not None:
            output.append(
                {
                    **shaped,
                    "jersey_number": assignment.jersey_number,
                    "position": assignment.position or player.position,
                    "roster_assignment": serialize(assignment),
                    "_identity_scope": scope,
                }
            )
    for player in players:
        if player.id in assigned_ids:
            continue
        if str((player.metadata_ or {}).get("team", "Toledo")).casefold() != "toledo":
            continue
        roster_season = (player.metadata_ or {}).get("roster_season")
        snapshot = (player.metadata_ or {}).get("roster_snapshot_date")
        if snapshot:
            try:
                snapshot_date = date.fromisoformat(str(snapshot))
            except ValueError:
                continue
            if recorded is None or recorded < snapshot_date:
                continue
        if season is not None and str(roster_season) != str(season):
            continue
        shaped = shape_player(player, VisibilityMode.STAFF, actor=user)
        if shaped:
            output.append(
                {
                    **shaped,
                    "_identity_scope": scope,
                    "roster_assignment": {
                        "source": "legacy_roster",
                        "season": roster_season,
                        "team": "Toledo",
                        "qualification": "unscoped" if season is None else "season_metadata",
                    },
                }
            )
    return output
