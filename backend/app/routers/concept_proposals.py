"""Embedding cluster review — zero-shot concept promotion (Issue #8 §10).

This router exposes the coach-review surface for HDBSCAN clusters over
``playembeddings``. Clusters live in ``embedding_cluster_proposals`` and
are *always* experimental until a coach accepts one. Endpoints:

* ``GET    /api/v1/concept-proposals``                — list pending / reviewed.
* ``GET    /api/v1/concept-proposals/{id}``           — single proposal.
* ``POST   /api/v1/concept-proposals``                — internal: nightly job
  emits a row per HDBSCAN cluster.
* ``POST   /api/v1/concept-proposals/{id}/accept``    — coach promotes; flips
  member embeddings ``is_experimental=false`` and records the chosen
  label name. Writing the downstream ``coach_corrections`` rows is left
  to the existing corrections router so the lineage matches the rest of
  the platform.
* ``POST   /api/v1/concept-proposals/{id}/reject``    — coach hides the
  cluster from future review.

Cluster proposals are deliberately *not* visible on production
dashboards. Other routers (search, self-scout, analytics export) must
keep filtering on ``is_experimental = false`` to honour the design-doc
governance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_analyst_or_above, require_coach_or_above
from app.models import (
    PLAY_EMBEDDING_DIM,
    EmbeddingClusterProposal,
    ModelVersion,
    PlayEmbedding,
    User,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/concept-proposals", tags=["concept-proposals"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class ProposalCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_version_id: uuid.UUID
    cluster_label: str = Field(..., max_length=64)
    proposed_name: str = Field(..., max_length=255)
    description: str | None = None
    member_clip_ids: list[uuid.UUID]
    centroid: list[float] | None = Field(
        default=None,
        min_length=PLAY_EMBEDDING_DIM,
        max_length=PLAY_EMBEDDING_DIM,
    )
    cohesion_score: float | None = None


class ProposalReview(BaseModel):
    label_name: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class ProposalResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    model_version_id: uuid.UUID
    cluster_label: str
    proposed_name: str
    description: str | None
    member_clip_ids: list[str]
    member_count: int
    cohesion_score: float | None
    status: str
    is_experimental: bool
    accepted_label_name: str | None
    reviewed_by: uuid.UUID | None
    reviewed_at: str | None
    review_notes: str | None
    created_at: str

    @classmethod
    def from_orm_proposal(cls, p: EmbeddingClusterProposal) -> ProposalResponse:
        return cls(
            id=p.id,
            model_version_id=p.model_version_id,
            cluster_label=p.cluster_label,
            proposed_name=p.proposed_name,
            description=p.description,
            member_clip_ids=[str(cid) for cid in (p.member_clip_ids or [])],
            member_count=p.member_count,
            cohesion_score=p.cohesion_score,
            status=p.status,
            is_experimental=p.is_experimental,
            accepted_label_name=p.accepted_label_name,
            reviewed_by=p.reviewed_by,
            reviewed_at=p.reviewed_at.isoformat() if p.reviewed_at else None,
            review_notes=p.review_notes,
            created_at=p.created_at.isoformat(),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ProposalResponse])
async def list_proposals(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ProposalResponse]:
    """List cluster proposals — default returns pending proposals first."""
    q = (
        select(EmbeddingClusterProposal)
        .order_by(
            EmbeddingClusterProposal.status.asc(),
            EmbeddingClusterProposal.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    if status_filter:
        q = q.where(EmbeddingClusterProposal.status == status_filter)
    result = await db.execute(q)
    return [ProposalResponse.from_orm_proposal(p) for p in result.scalars().all()]


@router.get("/{proposal_id}", response_model=ProposalResponse)
async def get_proposal(
    proposal_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ProposalResponse:
    p = await _load_proposal(db, proposal_id)
    return ProposalResponse.from_orm_proposal(p)


@router.post(
    "",
    response_model=ProposalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_proposal(
    payload: ProposalCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_analyst_or_above)],
) -> ProposalResponse:
    """Emit a single cluster proposal — called by the nightly discovery job.

    Restricted to analyst/admin so coaches can't accidentally create
    raw clusters from the review UI. The discovery job is expected to
    sweep proposals in batches and post one row per cluster.
    """
    mv = (
        await db.execute(select(ModelVersion).where(ModelVersion.id == payload.model_version_id))
    ).scalar_one_or_none()
    if mv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model version not found")

    proposal = EmbeddingClusterProposal(
        model_version_id=payload.model_version_id,
        cluster_label=payload.cluster_label,
        proposed_name=payload.proposed_name,
        description=payload.description,
        member_clip_ids=[mc for mc in payload.member_clip_ids],
        centroid=payload.centroid,
        member_count=len(payload.member_clip_ids),
        cohesion_score=payload.cohesion_score,
        status="pending",
        is_experimental=True,
    )
    db.add(proposal)
    await db.flush()
    await db.refresh(proposal)
    log.info(
        "concept_proposal_created",
        proposal_id=str(proposal.id),
        cluster_label=payload.cluster_label,
        member_count=proposal.member_count,
    )
    return ProposalResponse.from_orm_proposal(proposal)


@router.post(
    "/{proposal_id}/accept",
    response_model=ProposalResponse,
)
async def accept_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalReview,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ProposalResponse:
    """Promote a cluster to a coach-confirmed concept.

    Side effects:

    1. Marks the proposal ``status='accepted'``, ``is_experimental=False``.
    2. Flips ``is_experimental=False`` on every embedding belonging to
       the cluster's member clips (for the same model version).

    Writing the downstream ``coach_corrections`` rows is the caller's
    responsibility — typically the UI hands the proposal to the existing
    corrections form, which keeps the lineage path identical to manual
    relabels. We deliberately do *not* duplicate that here.
    """
    proposal = await _load_proposal(db, proposal_id)
    if proposal.status == "accepted":
        return ProposalResponse.from_orm_proposal(proposal)
    if proposal.status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal was previously rejected; reopen via discovery rerun.",
        )

    proposal.status = "accepted"
    proposal.is_experimental = False
    proposal.reviewed_by = current_user.id
    proposal.reviewed_at = datetime.now(UTC)
    proposal.accepted_label_name = payload.label_name or proposal.proposed_name
    proposal.review_notes = payload.notes

    if proposal.member_clip_ids:
        await db.execute(
            update(PlayEmbedding)
            .where(PlayEmbedding.clip_id.in_(proposal.member_clip_ids))
            .where(PlayEmbedding.model_version_id == proposal.model_version_id)
            .values(is_experimental=False)
        )

    await db.flush()
    await db.refresh(proposal)
    log.info(
        "concept_proposal_accepted",
        proposal_id=str(proposal.id),
        label_name=proposal.accepted_label_name,
        reviewer_id=str(current_user.id),
        member_count=proposal.member_count,
    )
    return ProposalResponse.from_orm_proposal(proposal)


@router.post(
    "/{proposal_id}/reject",
    response_model=ProposalResponse,
)
async def reject_proposal(
    proposal_id: uuid.UUID,
    payload: ProposalReview,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> ProposalResponse:
    """Hide a cluster from further review. Embedding rows stay experimental."""
    proposal = await _load_proposal(db, proposal_id)
    if proposal.status == "rejected":
        return ProposalResponse.from_orm_proposal(proposal)
    if proposal.status == "accepted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal is already accepted; cannot reject after promotion.",
        )

    proposal.status = "rejected"
    proposal.is_experimental = True
    proposal.reviewed_by = current_user.id
    proposal.reviewed_at = datetime.now(UTC)
    proposal.review_notes = payload.notes

    await db.flush()
    await db.refresh(proposal)
    log.info(
        "concept_proposal_rejected",
        proposal_id=str(proposal.id),
        reviewer_id=str(current_user.id),
    )
    return ProposalResponse.from_orm_proposal(proposal)


async def _load_proposal(db: AsyncSession, proposal_id: uuid.UUID) -> EmbeddingClusterProposal:
    proposal = (
        await db.execute(
            select(EmbeddingClusterProposal).where(EmbeddingClusterProposal.id == proposal_id)
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    return proposal
