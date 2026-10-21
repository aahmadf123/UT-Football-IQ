"""Authenticated staff view persistence, scoped to the local Toledo library."""

import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff
from app.models import SavedView, User

router = APIRouter(prefix="/api/v1/saved-views", tags=["saved-views"])
TEAM_SCOPE = "toledo"


class ViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    surface: Literal["film", "analytics"]
    visibility: Literal["private", "team"] = "private"
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("filters")
    @classmethod
    def bound_filters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > 32000:
            raise ValueError("Saved filters exceed 32 KB")
        return value


class ViewUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    visibility: Literal["private", "team"] | None = None
    filters: dict[str, Any] | None = None


def serialize(view: SavedView, user: User) -> dict[str, Any]:
    return {
        "id": str(view.id),
        "owner_id": str(view.owner_id),
        "can_edit": view.owner_id == user.id,
        "team_scope": view.team_scope,
        "name": view.name,
        "surface": view.surface,
        "visibility": view.visibility,
        "filters": view.filters,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


@router.get("")
async def list_views(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
    surface: Literal["film", "analytics"] | None = Query(default=None),
) -> list[dict[str, Any]]:
    stmt = select(SavedView).where(
        SavedView.team_scope == TEAM_SCOPE,
        or_(SavedView.owner_id == user.id, SavedView.visibility == "team"),
    )
    if surface:
        stmt = stmt.where(SavedView.surface == surface)
    rows = (await db.execute(stmt.order_by(SavedView.updated_at.desc()))).scalars().all()
    return [serialize(row, user) for row in rows]


@router.post("", status_code=201)
async def create_view(
    body: ViewCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    view = SavedView(id=uuid.uuid4(), owner_id=user.id, team_scope=TEAM_SCOPE, **body.model_dump())
    db.add(view)
    await db.flush()
    return serialize(view, user)


async def owned_view(db: AsyncSession, view_id: uuid.UUID, user: User) -> SavedView:
    row = (
        await db.execute(
            select(SavedView)
            .where(
                SavedView.id == view_id,
                SavedView.owner_id == user.id,
                SavedView.team_scope == TEAM_SCOPE,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Saved view not found")
    return row


@router.patch("/{view_id}")
async def update_view(
    view_id: uuid.UUID,
    body: ViewUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    row = await owned_view(db, view_id, user)
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "View fields cannot be null")
    try:
        validated = ViewCreate.model_validate(
            {
                "name": row.name,
                "surface": row.surface,
                "filters": row.filters,
                "visibility": row.visibility,
                **changes,
            }
        )
    except ValidationError as exc:
        raise HTTPException(422, "Invalid saved view fields") from exc
    for key in changes:
        setattr(row, key, getattr(validated, key))
    await db.flush()
    await db.refresh(row)
    return serialize(row, user)


@router.delete("/{view_id}", status_code=204)
async def delete_view(
    view_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> Response:
    row = await owned_view(db, view_id, user)
    await db.delete(row)
    await db.flush()
    return Response(status_code=204)
