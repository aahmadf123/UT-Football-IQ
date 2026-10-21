"""Visibility for preserved original clips after structural edits."""

import hashlib
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip


def active_clip() -> Any:
    return func.coalesce(Clip.provenance["state"].astext, "current") != "superseded"


def clip_is_active(clip: Clip) -> bool:
    return (clip.provenance or {}).get("state") != "superseded"


async def lock_boundary_scope(db: AsyncSession, scope_id: uuid.UUID) -> None:
    # Staff structural edits are infrequent. A deployment-wide edit guard also
    # serializes recording moves, whose old session scope can change while a
    # caller is discovering it. Scope locks remain useful for future sharding.
    await db.execute(select(func.pg_advisory_xact_lock(-719854339170563152)))
    key = int.from_bytes(
        hashlib.sha256(f"clip-boundaries:{scope_id}".encode()).digest()[:8], "big", signed=True
    )
    await db.execute(select(func.pg_advisory_xact_lock(key)))
