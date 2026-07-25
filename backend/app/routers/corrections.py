"""Coach corrections router — human overrides that become training labels."""

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.active_learning import (
    effective_priority,
    normalize_limit,
    queue_reason,
)
from app.database import get_db
from app.deps import get_current_user, require_analyst_or_above, require_coach_or_above
from app.models import Clip, CoachCorrection, CorrectionType, User

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/corrections", tags=["corrections"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class CorrectionCreate(BaseModel):
    clip_id: uuid.UUID
    correction_type: CorrectionType
    original_value: dict[str, Any] | None = None
    corrected_value: dict[str, Any]
    notes: str | None = None
    training_eligible: bool = True


class CorrectionResponse(BaseModel):
    id: uuid.UUID
    clip_id: uuid.UUID
    correction_type: str
    original_value: dict[str, Any] | None
    corrected_value: dict[str, Any]
    corrected_by: uuid.UUID
    notes: str | None
    training_eligible: bool
    exported_as_label: bool
    created_at: str

    @classmethod
    def from_orm_correction(cls, c: CoachCorrection) -> "CorrectionResponse":
        return cls(
            id=c.id,
            clip_id=c.clip_id,
            correction_type=c.correction_type.value,
            original_value=c.original_value,
            corrected_value=c.corrected_value,
            corrected_by=c.corrected_by,
            notes=c.notes,
            training_eligible=c.training_eligible,
            exported_as_label=c.exported_as_label,
            created_at=c.created_at.isoformat(),
        )


class ExportResponse(BaseModel):
    exported_count: int
    label_ids: list[uuid.UUID]
    training_dataset_id: uuid.UUID | None = None


class AnnotationQueueItem(BaseModel):
    """One prioritized review item — a view over a clip, not a new product.

    ``uncertainty_score`` is the stored calibrated entropy (NULL when the clip
    has not been scored yet). ``priority`` is the value the queue is ordered by
    (higher first; unscored clips sort last). ``uncertainty_calibrated`` lets the
    UI badge uncalibrated scores honestly (Issue #146).
    """

    model_config = {"protected_namespaces": ()}

    clip_id: uuid.UUID
    video_id: uuid.UUID
    play_number: int | None
    uncertainty_score: float | None
    uncertainty_calibrated: bool
    priority: float
    reason: str
    is_reviewed: bool
    created_at: str

    @classmethod
    def from_orm_clip(cls, c: Clip) -> "AnnotationQueueItem":
        return cls(
            clip_id=c.id,
            video_id=c.video_id,
            play_number=c.play_number,
            uncertainty_score=c.uncertainty_score,
            uncertainty_calibrated=c.uncertainty_calibrated,
            priority=effective_priority(c.uncertainty_score, c.uncertainty_calibrated),
            reason=queue_reason(c.uncertainty_score, c.uncertainty_calibrated),
            is_reviewed=c.is_reviewed,
            created_at=c.created_at.isoformat(),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/queue", response_model=list[AnnotationQueueItem])
async def get_annotation_queue(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
    strategy: str = Query(default="uncertainty", pattern="^(uncertainty|recent)$"),
    video_id: uuid.UUID | None = Query(default=None),
    include_unscored: bool = Query(default=True),
    limit: int = Query(default=25, ge=1, le=200),
) -> list[AnnotationQueueItem]:
    """Active-learning review queue, ordered by calibrated uncertainty (#145/#146).

    The labeled pool is excluded so the coach-correction loop stays the source
    of truth: a clip drops out once it is reviewed *or* has any coach correction.

    * ``strategy=uncertainty`` (default): most-uncertain clips first
      (``uncertainty_score`` desc, NULLs last). Unscored clips are never
      inflated into confident-looking scores — they sort last and are labeled
      ``reason="unscored"``.
    * ``strategy=recent``: newest unlabeled clips first (the prior default
      ordering), for coaches who just want to work through fresh film.

    ``include_unscored=false`` drops clips that have no uncertainty score yet.
    """
    capped = normalize_limit(limit)

    # Labeled pool = reviewed clips OR clips that already have a correction.
    has_correction = exists().where(CoachCorrection.clip_id == Clip.id)
    q = select(Clip).where(Clip.is_reviewed.is_(False), ~has_correction)
    if video_id is not None:
        q = q.where(Clip.video_id == video_id)
    if not include_unscored:
        q = q.where(Clip.uncertainty_score.is_not(None))

    if strategy == "uncertainty":
        q = q.order_by(Clip.uncertainty_score.desc().nullslast(), Clip.created_at.asc())
    else:  # recent
        q = q.order_by(Clip.created_at.desc())
    q = q.limit(capped)

    result = await db.execute(q)
    return [AnnotationQueueItem.from_orm_clip(c) for c in result.scalars().all()]


@router.post("", response_model=CorrectionResponse, status_code=status.HTTP_201_CREATED)
async def create_correction(
    body: CorrectionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> CorrectionResponse:
    """Submit a coach/analyst correction — recorded and eligible for training export."""
    clip_result = await db.execute(select(Clip).where(Clip.id == body.clip_id))
    if clip_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    correction = CoachCorrection(
        id=uuid.uuid4(),
        clip_id=body.clip_id,
        correction_type=body.correction_type,
        original_value=body.original_value,
        corrected_value=body.corrected_value,
        corrected_by=current_user.id,
        notes=body.notes,
        training_eligible=body.training_eligible,
    )
    db.add(correction)
    await db.flush()
    log.info(
        "correction_created",
        correction_id=str(correction.id),
        clip_id=str(body.clip_id),
        type=body.correction_type,
    )
    return CorrectionResponse.from_orm_correction(correction)


@router.get("", response_model=list[CorrectionResponse])
async def list_corrections(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
    clip_id: uuid.UUID | None = Query(default=None),
    exported: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CorrectionResponse]:
    """List corrections with optional filters."""
    q = (
        select(CoachCorrection)
        .order_by(CoachCorrection.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if clip_id is not None:
        q = q.where(CoachCorrection.clip_id == clip_id)
    if exported is not None:
        q = q.where(CoachCorrection.exported_as_label == exported)
    result = await db.execute(q)
    return [CorrectionResponse.from_orm_correction(c) for c in result.scalars().all()]


@router.post("/export", response_model=ExportResponse)
async def export_corrections_as_labels(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
    clip_id: uuid.UUID | None = Query(default=None),
    model_scope: str = Query(default="general"),
) -> ExportResponse:
    """Export un-exported corrections as Label rows for model training.

    Thin wrapper over :func:`app.services.corrections_export.export_corrections`
    — the nightly scheduler runs the same service without HTTP.
    """
    from app.services.corrections_export import export_corrections

    result = await export_corrections(db, clip_id=clip_id, model_scope=model_scope)
    return ExportResponse(
        exported_count=result.exported_count,
        label_ids=result.label_ids,
        training_dataset_id=result.training_dataset_id,
    )
