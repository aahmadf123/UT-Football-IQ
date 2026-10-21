"""Capture immutable server-owned observation bindings for reviewed events."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Clip, Label, Tracklet, Video
from app.services.actor_binding_protocol import (
    PROTOCOL,
    actor_references,
    observation_fingerprint,
    reference_shape_errors,
)


def track_observations(track: Tracklet) -> dict[str, Any]:
    return {
        "track_points": [
            {
                key: getattr(point, key)
                for key in (
                    "frame_number",
                    "timestamp_seconds",
                    "shot_id",
                    "visibility_state",
                    "bbox",
                )
            }
            for point in track.track_points
        ]
    }


async def verified_manual_identity(
    db: AsyncSession,
    clip_id: uuid.UUID,
    track: Tracklet,
    claim: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A real manual label, optionally followed through exact same-clip evidence."""
    await db.refresh(track, ["track_points"])
    if claim is not None and not isinstance(claim, dict):
        return None
    claim = claim or (track.provenance or {}).get("manual_identity_binding") or {}
    if not isinstance(claim, dict):
        return None
    direct = (
        (
            await db.execute(
                select(Label).where(
                    Label.tracklet_id == track.id,
                    Label.clip_id == clip_id,
                    Label.label_type == "player_identity",
                    Label.source.in_(["human", "manual"]),
                    Label.annotated_by.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if direct:
        # A later staff correction (including unassigning the player) outranks
        # any older inherited association carried in track provenance.
        if claim and any(
            str((label.label_value or {}).get("player_id")) != str(claim.get("player_id"))
            for label in direct
        ):
            return None
        claim = {}
    label_query = select(Label).where(
        Label.clip_id == clip_id,
        Label.label_type == "player_identity",
        Label.source.in_(["human", "manual"]),
        Label.annotated_by.is_not(None),
    )
    if claim:
        try:
            label_query = label_query.where(Label.id == uuid.UUID(str(claim.get("label_id"))))
        except (ValueError, TypeError):
            return None
    else:
        label_query = label_query.where(Label.tracklet_id == track.id)
    labels = (await db.execute(label_query)).scalars().all()
    proofs = []
    for label in labels:
        player_id = (label.label_value or {}).get("player_id")
        if not player_id or (claim and str(claim.get("player_id")) != str(player_id)):
            continue
        if not claim and str(track.player_id) != str(player_id):
            continue
        original = (
            await db.execute(
                select(Tracklet)
                .options(selectinload(Tracklet.track_points))
                .where(Tracklet.id == label.tracklet_id, Tracklet.clip_id == clip_id)
            )
        ).scalar_one_or_none()
        if original is None or str(original.player_id) != str(player_id):
            continue
        old_digest = observation_fingerprint(track_observations(original))
        if not old_digest or old_digest != observation_fingerprint(track_observations(track)):
            continue
        proofs.append(
            {
                "label_id": str(label.id),
                "source": label.source,
                "annotated_by": str(label.annotated_by),
                "player_id": str(player_id),
                "original_track_id": str(label.tracklet_id),
            }
        )
    return proofs[0] if len(proofs) == 1 else None


async def capture_actor_bindings(
    db: AsyncSession, clip: Clip, attrs: dict[str, Any]
) -> dict[str, Any]:
    refs = actor_references(attrs)
    video = (await db.execute(select(Video).where(Video.id == clip.video_id))).scalar_one()
    # Registration identity is explicitly separate from a full-media digest.
    # Until verified full identity is available, replay must abstain.
    from app.services.media_identity import verified_media_sha256

    media = await verified_media_sha256(db, video)
    result: dict[str, Any] = {
        "protocol": PROTOCOL,
        "scope": {
            "video_id": str(video.id),
            "clip_id": str(clip.id),
            "start_time": clip.start_time,
            "end_time": clip.end_time,
            "registration_fingerprint": video.content_sha256,
            "media_sha256": media,
        },
        "references": {},
        "review_required": [],
    }
    shape_errors = reference_shape_errors(attrs)
    if shape_errors:
        result["review_required"].append(
            {
                "reason": "actor_reference_container_invalid",
                "fields": shape_errors,
            }
        )
    if refs and (not video.content_sha256 or not media):
        result["review_required"].append({"reason": "actor_binding_source_identity_unverified"})
    for reference in sorted(refs):
        try:
            identity = uuid.UUID(reference)
        except (ValueError, TypeError):
            result["review_required"].append(
                {"actor_id": reference, "reason": "actor_reference_invalid"}
            )
            continue
        track = (
            await db.execute(
                select(Tracklet)
                .options(selectinload(Tracklet.track_points))
                .where(Tracklet.id == identity, Tracklet.clip_id == clip.id)
            )
        ).scalar_one_or_none()
        if (
            track is None
            or (track.provenance or {}).get("state") == "superseded"
            or (track.provenance or {}).get("publication_state") == "pending"
        ):
            result["review_required"].append(
                {"actor_id": reference, "reason": "actor_reference_not_active_in_clip"}
            )
            continue
        fingerprint = observation_fingerprint(track_observations(track))
        if fingerprint is None:
            result["review_required"].append(
                {"actor_id": reference, "reason": "actor_observations_incomplete"}
            )
            continue
        bound: dict[str, Any] = {"original_track_id": reference, "observation_sha256": fingerprint}
        proven = await verified_manual_identity(db, clip.id, track)
        if proven:
            # Retain the actual label's origin separately when staff reviews a
            # subsequent generation. It remains proof, not a newly made label.
            bound["manual_identity"] = {
                **proven,
                "label_track_id": proven["original_track_id"],
                "original_track_id": reference,
            }
        result["references"][reference] = bound
    return result
