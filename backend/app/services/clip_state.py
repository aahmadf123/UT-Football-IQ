"""Visibility for preserved original clips after structural edits."""

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Clip, ClipEdit, CoachCorrection, Event, Label, Metric, PlayView, Tracklet


def active_clip() -> Any:
    return func.coalesce(Clip.provenance["state"].astext, "current") != "superseded"


def clip_is_active(clip: Clip) -> bool:
    return (clip.provenance or {}).get("state") != "superseded"


def require_active_clip(clip: Clip) -> None:
    if not clip_is_active(clip):
        raise HTTPException(
            409, "This preserved clip was superseded; reload the active recording layout"
        )


async def lock_active_clip(db: AsyncSession, clip_id: uuid.UUID) -> Clip:
    clip = (
        await db.execute(select(Clip).where(Clip.id == clip_id).with_for_update())
    ).scalar_one_or_none()
    if clip is None:
        raise HTTPException(404, "Clip not found")
    require_active_clip(clip)
    return clip


async def layout_is_protected(db: AsyncSession, video_id: uuid.UUID, clips: Sequence[Clip]) -> bool:
    """Recutting must not orphan staff work or replace usable published results.

    Such recordings keep their exact windows/IDs while their analysis refreshes.
    Explicit staff split/merge remains the way to alter a protected layout.
    """
    if any(
        clip.boundary_source == "manual"
        or clip.is_reviewed
        or (clip.label_data or {}).get("_annotation_revision", 0)
        for clip in clips
    ):
        return True
    ids = [clip.id for clip in clips]
    track_ids = select(Tracklet.id).where(Tracklet.clip_id.in_(ids))
    checks = [
        select(ClipEdit.id).where(ClipEdit.video_id == video_id).exists(),
        select(CoachCorrection.id).where(CoachCorrection.clip_id.in_(ids)).exists(),
        select(Label.id)
        .where(
            or_(Label.clip_id.in_(ids), Label.tracklet_id.in_(track_ids)),
            Label.source.in_(("human", "manual", "inherited_human")),
        )
        .exists(),
        select(Event.id)
        .where(
            Event.clip_id.in_(ids),
            or_(
                Event.provenance["source"].astext.in_(("human", "manual")),
                Event.attributes["source"].astext.in_(("human", "manual")),
                Event.created_by.is_not(None) & Event.provenance["job_id"].astext.is_(None),
                (Event.provenance["source"].astext == "real")
                & (func.coalesce(Event.provenance["state"].astext, "current") != "superseded")
                & (
                    func.coalesce(Event.provenance["publication_state"].astext, "current")
                    != "pending"
                ),
            ),
        )
        .exists(),
        select(Metric.id)
        .where(
            Metric.clip_id.in_(ids),
            or_(
                Metric.provenance["source"].astext.in_(("manual", "human")),
                Metric.metric_value["provenance"]["source"].astext.in_(("manual", "human")),
            ),
        )
        .exists(),
        select(Metric.id)
        .where(
            Metric.clip_id.in_(ids),
            Metric.analytics_safe.is_(True),
            Metric.is_suppressed.is_(False),
            func.coalesce(Metric.provenance["state"].astext, "current") != "superseded",
            func.coalesce(Metric.provenance["publication_state"].astext, "current") != "pending",
        )
        .exists(),
        select(Tracklet.id)
        .where(
            Tracklet.clip_id.in_(ids),
            Tracklet.provenance["source"].astext.in_(("real", "manual", "human")),
            func.coalesce(Tracklet.provenance["state"].astext, "current") != "superseded",
            func.coalesce(Tracklet.provenance["publication_state"].astext, "current") != "pending",
        )
        .exists(),
        select(PlayView.id)
        .where(PlayView.clip_id.in_(ids), PlayView.match_source == "manual")
        .exists(),
    ]
    return any((await db.execute(select(*checks))).one())


async def lock_boundary_scope(db: AsyncSession, scope_id: uuid.UUID) -> None:
    # Staff structural edits are infrequent. A deployment-wide edit guard also
    # serializes recording moves, whose old session scope can change while a
    # caller is discovering it. Scope locks remain useful for future sharding.
    await db.execute(select(func.pg_advisory_xact_lock(-719854339170563152)))
    key = int.from_bytes(
        hashlib.sha256(f"clip-boundaries:{scope_id}".encode()).digest()[:8], "big", signed=True
    )
    await db.execute(select(func.pg_advisory_xact_lock(key)))
