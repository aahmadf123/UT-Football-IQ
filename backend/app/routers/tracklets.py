"""Tracklets router — player tracking data within clips."""

import uuid
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import Clip, CorrectionType, Label, Tracklet, TrackPoint, User
from app.services.annotations import submit_correction

log = structlog.get_logger(__name__)
router = APIRouter(tags=["tracklets"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class TrackPointSchema(BaseModel):
    id: uuid.UUID
    tracklet_id: uuid.UUID
    frame_number: int
    timestamp_seconds: float | None = None
    shot_id: str | None = None
    visibility_state: str | None = None
    field_x: float | None
    field_y: float | None
    bbox: list[float] | None
    detection_confidence: float | None

    @classmethod
    def from_orm(cls, tp: TrackPoint) -> "TrackPointSchema":
        return cls(
            id=tp.id,
            tracklet_id=tp.tracklet_id,
            frame_number=tp.frame_number,
            timestamp_seconds=tp.timestamp_seconds
            if isinstance(tp.timestamp_seconds, (int, float))
            else None,
            field_x=tp.field_x,
            field_y=tp.field_y,
            bbox=tp.bbox,
            detection_confidence=tp.detection_confidence,
            shot_id=tp.shot_id if isinstance(tp.shot_id, str) else None,
            visibility_state=tp.visibility_state if isinstance(tp.visibility_state, str) else None,
        )


class TrackletResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    provenance: dict[str, Any] | None = None

    id: uuid.UUID
    clip_id: uuid.UUID
    player_id: uuid.UUID | None
    start_frame: int
    end_frame: int
    track_confidence: float | None
    team_label: str | None
    model_version_id: uuid.UUID | None
    job_id: uuid.UUID | None
    created_at: str
    track_points: list[TrackPointSchema] = []

    @classmethod
    def from_orm_tracklet(cls, t: Tracklet, include_points: bool = False) -> "TrackletResponse":
        return cls(
            provenance=t.provenance if isinstance(t.provenance, dict) else None,
            id=t.id,
            clip_id=t.clip_id,
            player_id=t.player_id,
            start_frame=t.start_frame,
            end_frame=t.end_frame,
            track_confidence=t.track_confidence,
            team_label=t.team_label,
            model_version_id=t.model_version_id,
            job_id=t.job_id,
            created_at=t.created_at.isoformat(),
            track_points=[TrackPointSchema.from_orm(p) for p in t.track_points]
            if include_points
            else [],
        )


class TrackPointCreate(BaseModel):
    frame_number: int = Field(ge=0)
    timestamp_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    shot_id: str | None = Field(default=None, max_length=120)
    visibility_state: (
        Literal["visible", "observed", "occluded", "offscreen", "lost", "predicted", "unknown"]
        | None
    ) = None
    field_x: float | None = Field(default=None, allow_inf_nan=False)
    field_y: float | None = Field(default=None, allow_inf_nan=False)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    detection_confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("shot_id", mode="before")
    @classmethod
    def normalize_shot_id(cls, value: Any) -> str | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if value is None or isinstance(value, str):
            return value
        raise ValueError("Shot ID must be a string or integer")


class TrackletCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID | None = None
    provenance: dict[str, Any] | None = None

    clip_id: uuid.UUID
    player_id: uuid.UUID | None = None
    start_frame: int
    end_frame: int
    track_confidence: float | None = None
    team_label: str | None = None
    model_version_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    track_points: list[TrackPointCreate] = Field(default_factory=list, max_length=200000)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/v1/clips/{clip_id}/tracklets", response_model=list[TrackletResponse])
async def list_tracklets_for_clip(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> list[TrackletResponse]:
    """List all tracklets for a clip (summary — no points)."""
    clip_result = await db.execute(select(Clip).where(Clip.id == clip_id))
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    result = await db.execute(
        select(Tracklet)
        .where(
            Tracklet.clip_id == clip_id,
            func.coalesce(Tracklet.provenance["state"].as_string(), "") != "superseded",
            func.coalesce(Tracklet.provenance["publication_state"].as_string(), "") != "pending",
        )
        .order_by(Tracklet.start_frame)
    )
    return [
        TrackletResponse.from_orm_tracklet(t, include_points=False) for t in result.scalars().all()
    ]


@router.get("/api/v1/tracklets/{tracklet_id}", response_model=TrackletResponse)
async def get_tracklet(
    tracklet_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> TrackletResponse:
    """Get a single tracklet with all its track points."""
    result = await db.execute(
        select(Tracklet)
        .options(selectinload(Tracklet.track_points))
        .where(Tracklet.id == tracklet_id)
    )
    tracklet = result.scalar_one_or_none()
    if tracklet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracklet not found")
    return TrackletResponse.from_orm_tracklet(tracklet, include_points=True)


@router.post(
    "/api/v1/tracklets",
    response_model=TrackletResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tracklet(
    body: TrackletCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> TrackletResponse:
    """Store a new tracklet (with optional inline track points)."""
    clip_result = await db.execute(select(Clip).where(Clip.id == body.clip_id).with_for_update())
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    if body.id is not None:
        existing = (
            await db.execute(select(Tracklet).where(Tracklet.id == body.id))
        ).scalar_one_or_none()
        if existing is not None:
            if existing.clip_id != body.clip_id:
                raise HTTPException(409, "Operation ID belongs to another target")
            if body.job_id is not None and body.job_id != existing.job_id:
                raise HTTPException(409, "Output ID belongs to another result version")
            return TrackletResponse.from_orm_tracklet(existing)

    tracklet = Tracklet(
        id=body.id or uuid.uuid4(),
        provenance={
            **(body.provenance or {}),
            **({"publication_state": "pending"} if body.job_id else {}),
        },
        clip_id=body.clip_id,
        player_id=body.player_id,
        start_frame=body.start_frame,
        end_frame=body.end_frame,
        track_confidence=body.track_confidence,
        team_label=body.team_label,
        model_version_id=body.model_version_id,
        job_id=body.job_id,
    )
    db.add(tracklet)
    await db.flush()

    for point in body.track_points:
        pt = point.model_dump()
        tp = TrackPoint(
            id=uuid.uuid4(),
            tracklet_id=tracklet.id,
            frame_number=int(pt["frame_number"]),
            timestamp_seconds=pt.get("timestamp_seconds"),
            field_x=pt.get("field_x"),
            field_y=pt.get("field_y"),
            bbox=pt.get("bbox"),
            detection_confidence=pt.get("detection_confidence"),
            shot_id=pt.get("shot_id"),
            visibility_state=pt.get("visibility_state"),
        )
        db.add(tp)

    await db.flush()
    log.info("tracklet_created", tracklet_id=str(tracklet.id), clip_id=str(body.clip_id))

    # Re-fetch with points
    result = await db.execute(
        select(Tracklet)
        .options(selectinload(Tracklet.track_points))
        .where(Tracklet.id == tracklet.id)
    )
    tracklet = result.scalar_one()
    return TrackletResponse.from_orm_tracklet(tracklet, include_points=True)


class TrackletUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    player_id: uuid.UUID | None = None
    team_label: str | None = None
    track_confidence: float | None = None
    provenance: dict[str, Any] | None = None


@router.patch("/api/v1/tracklets/{tracklet_id}", response_model=TrackletResponse)
async def update_tracklet(
    tracklet_id: uuid.UUID,
    body: TrackletUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_any_staff)],
    worker_job_id: Annotated[str | None, Header(alias="X-Job-ID")] = None,
) -> TrackletResponse:
    """Update a tracklet's identity association or team label (Re-ID / coach correction)."""
    result = await db.execute(
        select(Tracklet)
        .options(selectinload(Tracklet.track_points))
        .where(Tracklet.id == tracklet_id)
    )
    tracklet = result.scalar_one_or_none()
    if tracklet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracklet not found")

    identity = body.model_dump(
        mode="json", exclude_unset=True, exclude={"track_confidence", "provenance"}
    )
    if identity and worker_job_id is None:
        await submit_correction(
            db,
            actor=_current_user,
            clip_id=tracklet.clip_id,
            correction_type=CorrectionType.player_identity,
            corrected_value={"tracklet_id": str(tracklet.id), **identity},
        )
    elif identity:
        manual = (
            await db.execute(
                select(Label)
                .where(
                    Label.tracklet_id == tracklet.id,
                    Label.label_type == "player_identity",
                    Label.source.in_(["human", "manual", "inherited_human"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if manual is None:
            if "player_id" in body.model_fields_set:
                tracklet.player_id = body.player_id
            if body.team_label is not None:
                tracklet.team_label = body.team_label
    if body.track_confidence is not None:
        tracklet.track_confidence = body.track_confidence
    if body.provenance is not None:
        tracklet.provenance = {**(tracklet.provenance or {}), **body.provenance}

    await db.flush()
    log.info("tracklet_updated", tracklet_id=str(tracklet_id))
    return TrackletResponse.from_orm_tracklet(tracklet, include_points=True)
