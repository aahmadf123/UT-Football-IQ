"""Play-embeddings router — write + list (Issue #8).

The retrieval endpoints live in ``backend/app/routers/search.py``; this
module only handles persistence and inspection. The GPU worker's
``stage_embed`` job calls ``POST /api/v1/embeddings/play`` once per clip
to upsert the fused embedding.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import (
    PLAY_EMBEDDING_CLIP_DIM,
    PLAY_EMBEDDING_DIM,
    PLAY_EMBEDDING_STRUCTURED_DIM,
    PLAY_EMBEDDING_VISUAL_DIM,
    Clip,
    ModelVersion,
    PlayEmbedding,
    User,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/embeddings", tags=["embeddings"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class PlayEmbeddingCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    clip_id: uuid.UUID
    model_version_id: uuid.UUID
    vector: list[float] = Field(..., min_length=PLAY_EMBEDDING_DIM, max_length=PLAY_EMBEDDING_DIM)
    visual_vector: list[float] | None = Field(
        default=None,
        min_length=PLAY_EMBEDDING_VISUAL_DIM,
        max_length=PLAY_EMBEDDING_VISUAL_DIM,
    )
    structured_vector: list[float] | None = Field(
        default=None,
        min_length=PLAY_EMBEDDING_STRUCTURED_DIM,
        max_length=PLAY_EMBEDDING_STRUCTURED_DIM,
    )
    # Raw 512-d CLIP image embedding in CLIP shared space (Issue #195).
    # Optional: only the real CLIP visual encoder produces it; the default
    # zero encoder leaves it unset and text search simply skips such rows.
    clip_vector: list[float] | None = Field(
        default=None,
        min_length=PLAY_EMBEDDING_CLIP_DIM,
        max_length=PLAY_EMBEDDING_CLIP_DIM,
    )
    chunk_kind: str = "play"
    snap_anchor: bool = True
    used_sam_masks: bool = False
    embedding_confidence: float | None = None
    source_label_ids: list[uuid.UUID] = Field(default_factory=list)
    calibration_version_id: uuid.UUID | None = None
    is_experimental: bool = True
    job_id: uuid.UUID | None = None


class PlayEmbeddingResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    clip_id: uuid.UUID
    chunk_kind: str
    snap_anchor: bool
    model_version_id: uuid.UUID
    calibration_version_id: uuid.UUID | None
    used_sam_masks: bool
    embedding_confidence: float | None
    is_experimental: bool
    source_label_ids: list[str]
    vector_dim: int
    # True when the raw CLIP image embedding is stored (Issue #195) — i.e.
    # this row is reachable by CLIP text-tower search (``/search/text``).
    has_clip_vector: bool
    created_at: str

    @classmethod
    def from_orm_embedding(cls, e: PlayEmbedding) -> PlayEmbeddingResponse:
        return cls(
            id=e.id,
            clip_id=e.clip_id,
            chunk_kind=e.chunk_kind,
            snap_anchor=e.snap_anchor,
            model_version_id=e.model_version_id,
            calibration_version_id=e.calibration_version_id,
            used_sam_masks=e.used_sam_masks,
            embedding_confidence=e.embedding_confidence,
            is_experimental=e.is_experimental,
            source_label_ids=[str(sid) for sid in (e.source_label_ids or [])],
            vector_dim=len(e.vector) if e.vector is not None else 0,
            has_clip_vector=e.clip_vector is not None,
            created_at=e.created_at.isoformat(),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/play",
    response_model=PlayEmbeddingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_play_embedding(
    payload: PlayEmbeddingCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> PlayEmbeddingResponse:
    """Upsert a play embedding for a clip.

    The unique key is ``(clip_id, chunk_kind, model_version_id)`` — see
    ``docs/embeddings-architecture.md`` §8. Re-running the embed stage
    with the same model version overwrites the prior row's vector and
    confidence; promoting to a new model version produces a *new* row so
    version-over-version comparisons stay possible.
    """
    if len(payload.vector) != PLAY_EMBEDDING_DIM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"vector must have exactly {PLAY_EMBEDDING_DIM} dimensions",
        )

    # Lineage sanity: the clip and the model version must exist.
    clip = (await db.execute(select(Clip).where(Clip.id == payload.clip_id))).scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    mv = (
        await db.execute(select(ModelVersion).where(ModelVersion.id == payload.model_version_id))
    ).scalar_one_or_none()
    if mv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model version not found")

    values: dict[str, Any] = {
        "clip_id": payload.clip_id,
        "chunk_kind": payload.chunk_kind,
        "snap_anchor": payload.snap_anchor,
        "vector": payload.vector,
        "visual_vector": payload.visual_vector,
        "structured_vector": payload.structured_vector,
        "clip_vector": payload.clip_vector,
        "model_version_id": payload.model_version_id,
        "calibration_version_id": payload.calibration_version_id,
        "source_label_ids": [sid for sid in payload.source_label_ids],
        "used_sam_masks": payload.used_sam_masks,
        "embedding_confidence": payload.embedding_confidence,
        "is_experimental": payload.is_experimental,
        "job_id": payload.job_id,
    }

    insert_stmt = pg_insert(PlayEmbedding).values(**values)
    upsert_stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_playembeddings_clip_chunk_modelversion",
        set_={
            "vector": insert_stmt.excluded.vector,
            "visual_vector": insert_stmt.excluded.visual_vector,
            "structured_vector": insert_stmt.excluded.structured_vector,
            "clip_vector": insert_stmt.excluded.clip_vector,
            "snap_anchor": insert_stmt.excluded.snap_anchor,
            "calibration_version_id": insert_stmt.excluded.calibration_version_id,
            "source_label_ids": insert_stmt.excluded.source_label_ids,
            "used_sam_masks": insert_stmt.excluded.used_sam_masks,
            "embedding_confidence": insert_stmt.excluded.embedding_confidence,
            "is_experimental": insert_stmt.excluded.is_experimental,
            "job_id": insert_stmt.excluded.job_id,
        },
    ).returning(PlayEmbedding)
    result = await db.execute(upsert_stmt)
    row = result.scalar_one()
    log.info(
        "play_embedding_upserted",
        clip_id=str(payload.clip_id),
        model_version_id=str(payload.model_version_id),
        chunk_kind=payload.chunk_kind,
        is_experimental=payload.is_experimental,
    )
    return PlayEmbeddingResponse.from_orm_embedding(row)


@router.get(
    "/play",
    response_model=list[PlayEmbeddingResponse],
)
async def list_play_embeddings(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
    clip_id: uuid.UUID | None = Query(default=None),
    model_version_id: uuid.UUID | None = Query(default=None),
    chunk_kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[PlayEmbeddingResponse]:
    """List embeddings (filter by clip / model_version / chunk_kind)."""
    q = select(PlayEmbedding).order_by(PlayEmbedding.created_at.desc()).limit(limit).offset(offset)
    if clip_id is not None:
        q = q.where(PlayEmbedding.clip_id == clip_id)
    if model_version_id is not None:
        q = q.where(PlayEmbedding.model_version_id == model_version_id)
    if chunk_kind is not None:
        q = q.where(PlayEmbedding.chunk_kind == chunk_kind)
    result = await db.execute(q)
    return [PlayEmbeddingResponse.from_orm_embedding(e) for e in result.scalars().all()]
