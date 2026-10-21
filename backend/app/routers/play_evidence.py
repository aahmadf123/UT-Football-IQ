"""Canonical play evidence and separate film/analysis coverage states."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff
from app.models import Clip, Event, Metric, Play, PlayView, Tracklet, User, Video
from app.routers.metrics import MetricResponse
from app.services.clip_state import clip_is_active
from app.services.metric_definitions import DEFINITION_VERSION, DEFINITIONS
from app.services.metric_policy import exclusion_reason

router = APIRouter(prefix="/api/v1/plays", tags=["play-evidence"])


@router.get("/{play_id}/evidence")
async def play_evidence(
    play_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    play = await db.get(Play, play_id)
    stmt = (
        select(Clip, Video, PlayView)
        .join(Video, Video.id == Clip.video_id)
        .outerjoin(PlayView, PlayView.clip_id == Clip.id)
    )
    stmt = stmt.where(PlayView.play_id == play_id) if play else stmt.where(Clip.id == play_id)
    views = list((await db.execute(stmt.order_by(Clip.created_at))).all())
    if not views:
        raise HTTPException(404, "Play not found")
    current_views = [row for row in views if clip_is_active(row[0])]
    if play and current_views:
        views = current_views
    clip_by_id = {clip.id: clip for clip, _video, _view in views}
    clips = [clip.id for clip, _video, _view in views]
    metrics = (
        (
            await db.execute(
                select(Metric).where(
                    Metric.clip_id.in_(clips), Metric.metric_name != "workload_fusion"
                )
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await db.execute(
                select(Event).where(Event.clip_id.in_(clips)).order_by(Event.timestamp_seconds)
            )
        )
        .scalars()
        .all()
    )
    tracks = (await db.execute(select(Tracklet).where(Tracklet.clip_id.in_(clips)))).scalars().all()
    current_tracks = [
        track
        for track in tracks
        if clip_is_active(clip_by_id[track.clip_id])
        and (track.provenance or {}).get("state") != "superseded"
        and (track.provenance or {}).get("publication_state") != "pending"
    ]

    def reason(metric: Metric) -> str | None:
        if not clip_is_active(clip_by_id[metric.clip_id]):
            return "superseded_clip"
        return exclusion_reason(metric) or ("experimental" if metric.experimental_flag else None)

    eligible = sum(reason(metric) is None for metric in metrics)
    return {
        "play_id": str(play.id) if play else str(play_id),
        "session_id": str(play.session_id)
        if play
        else str(views[0][1].practice_session_id)
        if views[0][1].practice_session_id
        else None,
        "title": play.title if play else None,
        "definition_version": DEFINITION_VERSION,
        "historical": not bool(current_views),
        "views": [
            {
                "clip_id": str(clip.id),
                "video_id": str(video.id),
                "start_seconds": clip.start_time,
                "end_seconds": clip.end_time,
                "camera_label": video.camera_label,
                "result_state": clip.result_state,
                "alignment_offset_seconds": view.alignment_offset_seconds if view else None,
                "provenance": clip.provenance,
                "video_provenance": video.metadata_,
            }
            for clip, video, view in views
        ],
        "metrics": [
            {
                **MetricResponse.from_orm_metric(metric).model_dump(mode="json"),
                "eligibility": "eligible" if reason(metric) is None else "excluded",
                "exclusion_reason": reason(metric),
                "definition": DEFINITIONS.get(metric.metric_name),
            }
            for metric in metrics
        ],
        "events": [
            {
                "id": str(event.id),
                "clip_id": str(event.clip_id),
                "event_type": event.event_type,
                "timestamp_seconds": event.timestamp_seconds,
                "attributes": event.attributes,
                "provenance": event.provenance,
            }
            for event in events
        ],
        "identities": [
            {
                "tracklet_id": str(track.id),
                "clip_id": str(track.clip_id),
                "player_id": str(track.player_id) if track.player_id else None,
                "team_label": track.team_label,
                "provenance": track.provenance,
            }
            for track in current_tracks
        ],
        "readiness": {
            "film": bool(views),
            "cuts": True,
            "tracking": bool(current_tracks),
            "historical_or_pending_tracks": len(tracks) - len(current_tracks),
            "identity_coverage": sum(track.player_id is not None for track in current_tracks)
            / len(current_tracks)
            if current_tracks
            else None,
            "analytics_coverage": eligible / len(metrics) if metrics else None,
            "eligible_measurements": eligible,
            "excluded_measurements": len(metrics) - eligible,
        },
    }
