"""Events router — game events detected within clips (Stage 7 output).

Supported event_type values (not enforced by the DB, but documented here):
    snap, motion_start, motion_end, handoff, mesh_point, throw (forward-pass release),
    catch, target, contact, tackle, end_of_play
"""

import uuid
from collections.abc import Sequence
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff, require_coach_or_above
from app.models import Clip, CorrectionType, Event, User
from app.services.annotations import submit_correction
from app.services.metric_policy import INVALID_SOURCES

log = structlog.get_logger(__name__)
router = APIRouter(tags=["events"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class EventCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID | None = None
    provenance: dict[str, Any] | None = None
    job_id: uuid.UUID | None = None

    clip_id: uuid.UUID
    event_type: str
    frame_number: int | None = None
    timestamp_seconds: float | None = None
    attributes: dict[str, Any] | None = None
    model_version_id: uuid.UUID | None = None


class EventUpdate(BaseModel):
    event_type: str | None = None
    frame_number: int | None = None
    timestamp_seconds: float | None = None
    attributes: dict[str, Any] | None = None


class EventResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    provenance: dict[str, Any] | None = None

    id: uuid.UUID
    clip_id: uuid.UUID
    event_type: str
    frame_number: int | None
    timestamp_seconds: float | None
    attributes: dict[str, Any] | None
    created_by: uuid.UUID | None
    model_version_id: uuid.UUID | None
    created_at: str

    @classmethod
    def from_orm_event(cls, e: Event) -> "EventResponse":
        return cls(
            provenance=e.provenance if isinstance(e.provenance, dict) else None,
            id=e.id,
            clip_id=e.clip_id,
            event_type=e.event_type,
            frame_number=e.frame_number,
            timestamp_seconds=e.timestamp_seconds,
            attributes=e.attributes,
            created_by=e.created_by,
            model_version_id=e.model_version_id,
            created_at=e.created_at.isoformat(),
        )


def effective_events(events: Sequence[Event]) -> list[Event]:
    events = [
        event
        for event in events
        if (event.provenance or {}).get("state") != "superseded"
        and (event.provenance or {}).get("publication_state") != "pending"
    ]

    def is_manual(event: Event) -> bool:
        return (
            isinstance(event.provenance, dict)
            and event.provenance.get("source") in {"manual", "human"}
        ) or (
            isinstance(event.attributes, dict)
            and event.attributes.get("source") in {"manual", "human"}
        )

    manual_types = {event.event_type for event in events if is_manual(event)}
    return [event for event in events if is_manual(event) or event.event_type not in manual_types]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/v1/clips/{clip_id}/events", response_model=list[EventResponse])
async def list_events_for_clip(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
    event_type: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[EventResponse]:
    """List events for a clip ordered by frame number, with optional type filter."""
    clip_result = await db.execute(select(Clip).where(Clip.id == clip_id))
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    q = (
        select(Event)
        .where(
            Event.clip_id == clip_id,
            func.coalesce(Event.provenance["state"].as_string(), "") != "superseded",
            ~func.coalesce(Event.provenance["source"].as_string(), "unknown").in_(INVALID_SOURCES),
        )
        .order_by(Event.frame_number)
        .limit(limit)
        .offset(offset)
    )
    if event_type is not None:
        q = q.where(Event.event_type == event_type)
    result = await db.execute(q)
    return [EventResponse.from_orm_event(e) for e in effective_events(result.scalars().all())]


@router.post(
    "/api/v1/events",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_event(
    body: EventCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_any_staff)],
) -> EventResponse:
    """Record a new game event (model-detected or manually tagged)."""
    clip_result = await db.execute(select(Clip).where(Clip.id == body.clip_id).with_for_update())
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    if body.id is not None:
        existing = (await db.execute(select(Event).where(Event.id == body.id))).scalar_one_or_none()
        if existing is not None:
            if existing.clip_id != body.clip_id:
                raise HTTPException(409, "Operation ID belongs to another target")
            incoming_job = body.job_id or (body.provenance or {}).get("job_id")
            if incoming_job is not None and (existing.provenance or {}).get("job_id") != str(
                incoming_job
            ):
                raise HTTPException(409, "Output ID belongs to another result version")
            return EventResponse.from_orm_event(existing)

    provenance = dict(body.provenance or {})
    if body.job_id is not None:
        provenance["job_id"] = str(body.job_id)
    job_id = provenance.get("job_id")
    if job_id and provenance.get("source") not in {"manual", "human"}:
        provenance["publication_state"] = "pending"
    event = Event(
        id=body.id or uuid.uuid4(),
        provenance=provenance or None,
        clip_id=body.clip_id,
        event_type=body.event_type,
        frame_number=body.frame_number,
        timestamp_seconds=body.timestamp_seconds,
        attributes=body.attributes,
        created_by=current_user.id,
        model_version_id=body.model_version_id,
    )
    db.add(event)
    await db.flush()
    log.info(
        "event_created",
        event_id=str(event.id),
        clip_id=str(body.clip_id),
        event_type=body.event_type,
    )
    return EventResponse.from_orm_event(event)


@router.get("/api/v1/events/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> EventResponse:
    """Get a single event record."""
    result = await db.execute(select(Event).where(Event.id == event_id))
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return EventResponse.from_orm_event(event)


@router.patch("/api/v1/events/{event_id}", response_model=EventResponse)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
) -> EventResponse:
    """Manually correct a game event (coach/analyst override)."""
    result = await db.execute(select(Event).where(Event.id == event_id))
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    await submit_correction(
        db,
        actor=_current_user,
        clip_id=event.clip_id,
        correction_type=CorrectionType.event_tag,
        corrected_value={"event_id": str(event.id), **body.model_dump(exclude_unset=True)},
    )
    await db.flush()
    log.info("event_updated", event_id=str(event_id))
    return EventResponse.from_orm_event(event)


@router.delete("/api/v1/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
) -> None:
    """Delete a game event (e.g. false positive from model detection)."""
    result = await db.execute(select(Event).where(Event.id == event_id))
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    await db.delete(event)
    await db.flush()
    log.info("event_deleted", event_id=str(event_id))
