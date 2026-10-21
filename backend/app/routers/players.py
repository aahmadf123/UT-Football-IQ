"""Players router — roster identity with visibility-mode shaping.

This is the P1 read surface for the player domain.  The endpoint is shared
between staff (full identity payload), player self-views, and external
recruiting consumers — visibility is selected per-request via ``?mode=`` and
enforced server-side by :mod:`app.governance`.

Endpoints:
    GET   /api/v1/players                       — list (filtered + mode shaped)
    GET   /api/v1/players/{id}                  — single player (mode shaped)
    PATCH /api/v1/players/{id}/visibility       — change visibility state
    GET   /api/v1/players/{id}/visibility/audit — visibility change history

Sensitive metrics (pose, biomechanics, health, alerts) are *never* returned
here; they live on dedicated routers that perform their own RBAC.  The
visibility shaping in this router only governs identity-level fields.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cfbd.roster_sync import STAFF_LOCKABLE_FIELDS
from app.database import get_db
from app.deps import require_coach_or_above
from app.governance import (
    Action,
    Resource,
    VisibilityMode,
    VisibilityState,
    audit_event,
    policy_allows,
    require_policy,
    resolve_visibility_mode,
    shape_player,
)
from app.models import Player, PlayerVisibilityAudit, PlayerVisibilityState, User

log = structlog.get_logger(__name__)
router = APIRouter(tags=["players"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class VisibilityUpdate(BaseModel):
    """Request body for :func:`update_visibility`."""

    state: VisibilityState
    reason: str | None = None


class VisibilityAuditEntry(BaseModel):
    id: uuid.UUID
    player_id: uuid.UUID
    previous_state: str | None
    next_state: str
    actor_id: uuid.UUID | None
    reason: str | None
    created_at: str


_POSITION_GROUPS = {
    "qb": "QB",
    "skill": "Skill",
    "rb": "RB",
    "wr": "WR",
    "te": "TE",
    "ol": "OL",
    "dl": "DL",
    "lb": "LB",
    "db": "DB",
    "st": "ST",
}
_POSITION_TO_GROUP = {
    "QB": "QB",
    "RB": "RB",
    "FB": "RB",
    "HB": "RB",
    "WR": "WR",
    "SLOT": "WR",
    "TE": "TE",
    "OT": "OL",
    "OG": "OL",
    "C": "OL",
    "G": "OL",
    "T": "OL",
    "OL": "OL",
    "DT": "DL",
    "DE": "DL",
    "NT": "DL",
    "DL": "DL",
    "LB": "LB",
    "ILB": "LB",
    "OLB": "LB",
    "CB": "DB",
    "S": "DB",
    "SAF": "DB",
    "FS": "DB",
    "SS": "DB",
    "DB": "DB",
    "K": "ST",
    "PK": "ST",
    "P": "ST",
    "LS": "ST",
    "ST": "ST",
}


class PlayerCreate(BaseModel):
    id: uuid.UUID | None = None
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    jersey_number: int | None = Field(default=None, ge=0, le=99)
    position: str | None = Field(default=None, max_length=50)
    position_group: str | None = None
    is_active: bool = True

    @field_validator("first_name", "last_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Player name cannot be empty")
        return value

    @field_validator("position")
    @classmethod
    def clean_position(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z /-]*", value):
            raise ValueError("Position must be a football position name")
        return value

    @field_validator("position_group")
    @classmethod
    def clean_group(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if value.strip().lower() not in _POSITION_GROUPS:
            raise ValueError("Position group must be QB, RB, WR, TE, Skill, OL, DL, LB, DB, or ST")
        return _POSITION_GROUPS[value.strip().lower()]


class PlayerUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    jersey_number: int | None = Field(default=None, ge=0, le=99)
    position: str | None = Field(default=None, max_length=50)
    position_group: str | None = None
    is_active: bool | None = None


class RosterImport(BaseModel):
    csv: str = Field(max_length=2 * 1024 * 1024)
    dry_run: bool = True


async def _roster_lock(db: AsyncSession) -> None:
    # Serialize UI imports and edits. Numbers remain candidate evidence.
    await db.execute(select(func.pg_advisory_xact_lock(71420961014)))


async def _roster_conflict(
    db: AsyncSession, values: PlayerCreate, exclude_id: uuid.UUID | None = None
) -> Player | None:
    if not values.is_active:
        return None
    stmt = select(Player).where(
        Player.is_active.is_(True),
        func.lower(Player.first_name) == values.first_name.lower(),
        func.lower(Player.last_name) == values.last_name.lower(),
    )
    if exclude_id:
        stmt = stmt.where(Player.id != exclude_id)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


@router.post("/api/v1/players", status_code=201)
async def create_player(
    body: PlayerCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    await _roster_lock(db)
    if body.id:
        existing = await db.get(Player, body.id)
        if existing:
            if existing.first_name != body.first_name or existing.last_name != body.last_name:
                raise HTTPException(409, "Player ID already belongs to another identity")
            return shape_player(existing, VisibilityMode.STAFF, actor=current_user) or {}
    conflict = await _roster_conflict(db, body)
    if conflict:
        raise HTTPException(
            409,
            {
                "error_code": "roster_identity_conflict",
                "player_id": str(conflict.id),
                "message": (
                    "An active roster identity already has this name. "
                    "Edit that player explicitly or use its stable player ID."
                ),
            },
        )
    values = body.model_dump(exclude={"id"})
    values["position_group"] = body.position_group or _POSITION_TO_GROUP.get(body.position or "")
    player = Player(id=body.id or uuid.uuid4(), **values)
    try:
        async with db.begin_nested():
            db.add(player)
            await db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "Roster identity conflicts with an existing player") from exc
    return shape_player(player, VisibilityMode.STAFF, actor=current_user) or {}


# Literal import route is registered before the generic player-id GET route.
@router.post("/api/v1/players/import")
async def import_roster(
    body: RosterImport,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    await _roster_lock(db)
    reader = csv.DictReader(io.StringIO(body.csv.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise HTTPException(422, "CSV header is required")
    reader.fieldnames = [key.strip().lower().replace(" ", "_") for key in reader.fieldnames]
    if not {"first_name", "last_name"}.issubset(reader.fieldnames):
        raise HTTPException(422, "CSV must have first_name and last_name columns")
    raw_rows = list(reader)
    if len(raw_rows) > 5000:
        raise HTTPException(413, "Import at most 5000 players at once")
    results = []
    errors = []
    created = 0
    skipped = 0
    seen = set()
    for line, raw in enumerate(raw_rows, start=2):
        values = {
            key: value.strip()
            for key, value in raw.items()
            if key and isinstance(value, str) and value.strip() and key in PlayerCreate.model_fields
        }
        try:
            payload = PlayerCreate.model_validate(values)
        except ValidationError as exc:
            error = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            results.append({"line": line, "status": "invalid", "error": error})
            errors.append({"line": line, "error": error})
            continue
        group = payload.position_group or _POSITION_TO_GROUP.get(payload.position or "")
        key = (
            payload.first_name.casefold(),
            payload.last_name.casefold(),
            payload.jersey_number,
            group,
        )
        existing = (
            await db.execute(
                select(Player)
                .where(
                    func.lower(Player.first_name) == payload.first_name.lower(),
                    func.lower(Player.last_name) == payload.last_name.lower(),
                    Player.jersey_number.is_(None)
                    if payload.jersey_number is None
                    else Player.jersey_number == payload.jersey_number,
                    Player.position_group.is_(None)
                    if group is None
                    else Player.position_group == group,
                )
                .order_by(Player.is_active.desc(), Player.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing or key in seen:
            skipped += 1
            result = {"line": line, "status": "existing"}
            if existing:
                result["player"] = shape_player(existing, VisibilityMode.STAFF, actor=current_user)
            results.append(result)
            continue
        conflict = await _roster_conflict(db, payload)
        if conflict is None:
            conflict = (
                await db.execute(
                    select(Player)
                    .where(
                        Player.is_active.is_(True),
                        func.lower(Player.first_name) == payload.first_name.lower(),
                        func.lower(Player.last_name) == payload.last_name.lower(),
                        Player.position_group.is_(None)
                        if group is None
                        else Player.position_group == group,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        if payload.id:
            id_row = await db.get(Player, payload.id)
            if id_row:
                conflict = id_row
        if conflict:
            error = "Existing roster identity conflicts; edit that player explicitly."
            results.append(
                {
                    "line": line,
                    "status": "conflict",
                    "player": shape_player(conflict, VisibilityMode.STAFF, actor=current_user),
                    "error": error,
                }
            )
            errors.append({"line": line, "error": error})
            continue
        seen.add(key)
        if body.dry_run:
            results.append(
                {"line": line, "status": "create", "player": payload.model_dump(mode="json")}
            )
            created += 1
        else:
            player = await create_player(payload, db, current_user)
            results.append({"line": line, "status": "create", "player": player})
            created += 1
    return {
        "dry_run": body.dry_run,
        "created": created,
        "updated": 0,
        "skipped": skipped,
        "rows": results,
        "errors": errors,
    }


@router.patch("/api/v1/players/{player_id}")
async def update_player(
    player_id: uuid.UUID,
    body: PlayerUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    await _roster_lock(db)
    player = (
        await db.execute(select(Player).where(Player.id == player_id).with_for_update())
    ).scalar_one_or_none()
    if player is None:
        raise HTTPException(404, "Player not found")
    changes = body.model_dump(exclude_unset=True)
    try:
        validated = PlayerCreate(
            **{
                name: changes.get(name, getattr(player, name))
                for name in PlayerCreate.model_fields
                if name != "id"
            }
        )
    except ValidationError as exc:
        raise HTTPException(422, "Player identity fields are invalid") from exc
    if await _roster_conflict(db, validated, player_id):
        raise HTTPException(409, "Another active player has that roster identity")
    effective = validated.model_dump(exclude={"id"})
    # A coach's roster edit (practice jersey, moved position, active status)
    # must survive the weekly CFBD roster sync, which skips locked fields.
    # The editor submits every field, so lock only values that changed.
    edited = sorted(
        name
        for name in changes
        if name in STAFF_LOCKABLE_FIELDS and effective[name] != getattr(player, name)
    )
    for name in changes:
        setattr(player, name, effective[name])
    if edited:
        metadata = dict(player.metadata_ or {})
        metadata["staff_locked_fields"] = sorted(
            set(metadata.get("staff_locked_fields") or []) | set(edited)
        )
        player.metadata_ = metadata
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "Roster identity conflicts with an existing player") from exc
    return shape_player(player, VisibilityMode.STAFF, actor=current_user) or {}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/v1/players")
async def list_players(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAYER_PROFILE, Action.READ))],
    position: Annotated[
        str | None,
        Query(description="Exact match on the ``position`` column (e.g. ``WR``)."),
    ] = None,
    position_group: Annotated[
        str | None,
        Query(
            description=(
                "Coaching group (``QB``, ``Skill``, ``OL``, ``DL``, ``LB``, ``DB``, ``ST``)."
            ),
        ),
    ] = None,
    is_active: Annotated[
        bool | None,
        Query(description="Filter by active-roster flag. Omit to include both."),
    ] = None,
    jersey_number: Annotated[
        int | None,
        Query(description="Exact jersey number match."),
    ] = None,
    search: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=80,
            description="Case-insensitive substring match on first or last name.",
        ),
    ] = None,
    mode: Annotated[
        VisibilityMode | None,
        Query(description="Visibility mode override (staff|player|recruiting)."),
    ] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    """Return the roster shaped for the caller's effective visibility mode.

    Ordering is deterministic — jersey number ascending (NULLs last), then
    last name, first name, and finally id.  Players whose visibility state
    excludes them from the resolved mode are silently filtered out (rather
    than returning a redacted stub) so list views don't leak that a record
    exists at all.
    """
    effective_mode = resolve_visibility_mode(current_user, mode)
    audit_event(
        "audit.visibility.applied",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        effective_mode=effective_mode.value,
        endpoint="players.list",
    )

    stmt = select(Player)
    if position is not None:
        stmt = stmt.where(Player.position == position)
    if position_group is not None:
        stmt = stmt.where(Player.position_group == position_group)
    if is_active is not None:
        stmt = stmt.where(Player.is_active == is_active)
    if jersey_number is not None:
        stmt = stmt.where(Player.jersey_number == jersey_number)
    if search is not None:
        needle = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                Player.first_name.ilike(needle),
                Player.last_name.ilike(needle),
            )
        )
    stmt = (
        stmt.order_by(
            Player.jersey_number.asc().nulls_last(),
            Player.last_name.asc(),
            Player.first_name.asc(),
            Player.id.asc(),
        )
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(stmt)
    out: list[dict[str, Any]] = []
    for p in result.scalars().all():
        shaped = shape_player(p, effective_mode, actor=current_user)
        if shaped is not None:
            out.append(shaped)
    return out


@router.get("/api/v1/players/{player_id}")
async def get_player(
    player_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAYER_PROFILE, Action.READ))],
    mode: Annotated[
        VisibilityMode | None,
        Query(description="Visibility mode override (staff|player|recruiting)."),
    ] = None,
) -> dict[str, Any]:
    """Return a single player record shaped for the caller's mode."""
    effective_mode = resolve_visibility_mode(current_user, mode)

    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Player not found",
        )

    shaped = shape_player(player, effective_mode, actor=current_user)
    if shaped is None:
        # Return 404 so non-staff callers can't tell whether the record exists
        # or simply isn't approved for their view.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Player not found",
        )
    audit_event(
        "audit.visibility.applied",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        effective_mode=effective_mode.value,
        player_id=player.id,
        endpoint="players.detail",
    )
    return shaped


@router.patch("/api/v1/players/{player_id}/visibility")
async def update_visibility(
    player_id: uuid.UUID,
    body: VisibilityUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[
        User, Depends(require_policy(Resource.PLAYER_VISIBILITY, Action.WRITE))
    ],
) -> dict[str, Any]:
    """Change a player's visibility state.

    * Coaches/analysts/admins may transition to/from ``staff_only`` and
      ``player_approved`` and may archive.
    * Approving for recruiting requires the additional ``APPROVE`` policy
      (admin/analyst only) — recruiting exposes the player externally so it
      gets a tighter approver list.
    """
    target_state = body.state
    if target_state == VisibilityState.recruiting_approved and not policy_allows(
        current_user.role, Resource.PLAYER_VISIBILITY, Action.APPROVE
    ):
        audit_event(
            "audit.access.denied",
            actor_id=current_user.id,
            actor_role=current_user.role.value,
            resource=Resource.PLAYER_VISIBILITY.value,
            action=Action.APPROVE.value,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "approval_required",
                "message": ("Recruiting approval requires admin or analyst role."),
            },
        )

    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")

    previous_model = player.visibility_state or PlayerVisibilityState.staff_only
    previous = VisibilityState(previous_model.value)
    next_state_model = PlayerVisibilityState(target_state.value)

    player.visibility_state = next_state_model
    player.visibility_updated_by = current_user.id
    player.visibility_updated_at = datetime.now(UTC)

    audit_row = PlayerVisibilityAudit(
        id=uuid.uuid4(),
        player_id=player.id,
        previous_state=PlayerVisibilityState(previous.value),
        next_state=next_state_model,
        actor_id=current_user.id,
        reason=body.reason,
    )
    db.add(audit_row)
    await db.flush()

    audit_event(
        "audit.visibility.changed",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        player_id=player.id,
        previous_state=previous.value,
        next_state=next_state_model.value,
    )

    return {
        "player_id": str(player.id),
        "previous_state": previous.value,
        "next_state": next_state_model.value,
        "updated_by": str(current_user.id),
        "updated_at": player.visibility_updated_at.isoformat(),
    }


@router.get("/api/v1/players/{player_id}/visibility/audit")
async def get_visibility_audit(
    player_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[
        User, Depends(require_policy(Resource.PLAYER_VISIBILITY, Action.WRITE))
    ],
    limit: int = Query(default=50, ge=1, le=200),
) -> list[VisibilityAuditEntry]:
    """Return the visibility transition history for a player (staff only)."""
    stmt = (
        select(PlayerVisibilityAudit)
        .where(PlayerVisibilityAudit.player_id == player_id)
        .order_by(PlayerVisibilityAudit.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [
        VisibilityAuditEntry(
            id=row.id,
            player_id=row.player_id,
            previous_state=row.previous_state.value if row.previous_state else None,
            next_state=row.next_state.value,
            actor_id=row.actor_id,
            reason=row.reason,
            created_at=row.created_at.isoformat(),
        )
        for row in result.scalars().all()
    ]
