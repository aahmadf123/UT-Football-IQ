"""iPad one-tap correction sync router (Issue #16).

Endpoints:
  POST /api/v1/corrections/sync — bulk-sync corrections from the iPad
                                   coaching app during or after a practice

Synced corrections feed the existing ``coach_corrections`` table.  A new
corrections table is NOT created — this router calls the same DB model
as the existing corrections router.

Use cases:
  - Coach taps "wrong formation" on iPad → sync applies the correction
  - Coach taps "wrong player" on clip → synced at end of period
  - Offline corrections collected while drone connectivity is down →
    synced when connectivity restores

The iPad client should call this endpoint with a list of correction dicts.
Each dict applies the same transactional edit and history as a manual
correction. Stable client operation IDs identify retries across restarts;
expected revisions detect conflicts. A rejected item rolls back its batch.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_coach_or_above
from app.models import CorrectionType, User
from app.services.annotations import submit_correction

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/corrections", tags=["corrections"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class CorrectionSyncItem(BaseModel):
    """One correction from the iPad queue."""

    clip_id: uuid.UUID
    correction_type: CorrectionType
    original_value: dict[str, Any] | None = None
    corrected_value: dict[str, Any]
    notes: str | None = None
    training_eligible: bool = True
    client_timestamp: str | None = None
    client_operation_id: uuid.UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class CorrectionSyncRequest(BaseModel):
    """Batch of corrections from the iPad coaching app."""

    corrections: list[CorrectionSyncItem] = Field(max_length=200)
    device_id: str | None = None
    session_id: str | None = None


class CorrectionSyncResponse(BaseModel):
    synced_count: int
    skipped_count: int
    correction_ids: list[uuid.UUID]


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/sync", response_model=CorrectionSyncResponse, status_code=status.HTTP_201_CREATED)
async def sync_corrections(
    body: CorrectionSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> CorrectionSyncResponse:
    """Bulk-sync iPad one-tap corrections to the coach_corrections table.

    Applies every correction atomically. Stable client operation IDs identify
    retries without discarding different corrections made close together.
    Invalid targets or revision conflicts roll back the entire batch.

    Returns a count of synced and skipped corrections plus the new IDs.
    """
    synced: list[uuid.UUID] = []
    skipped = 0
    for item in body.corrections:
        # Stable client operation IDs deduplicate retries indefinitely, including
        # after restart. Distinct corrections within one second remain distinct.
        payload = item.model_dump(exclude={"client_timestamp"})
        correction, replayed = await submit_correction(db, actor=current_user, **payload)
        if replayed:
            skipped += 1
        else:
            synced.append(correction.id)

    log.info(
        "correction_sync_complete",
        synced=len(synced),
        skipped=skipped,
        session_id=body.session_id,
        device_id=body.device_id,
    )
    return CorrectionSyncResponse(
        synced_count=len(synced),
        skipped_count=skipped,
        correction_ids=synced,
    )
