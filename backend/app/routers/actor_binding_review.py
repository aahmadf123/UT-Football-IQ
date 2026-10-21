"""Staff replay diagnostics stored separately from human annotations."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_any_staff
from app.models import (
    Clip,
    Event,
    JobStatus,
    JobType,
    ProcessingJob,
    SystemSetting,
    User,
    WorkerRuntime,
)
from app.routers.events import EventResponse, effective_events
from app.services.actor_binding_protocol import PROTOCOL, actor_references
from app.services.annotations import annotation_revision
from app.services.clip_state import require_active_clip

router = APIRouter(tags=["events"])
PREDICTION_CLOCK_FAMILIES = {
    "snap": "snap",
    "pass_release": "pass_release",
    "forward_pass_release": "pass_release",
    "throw": "pass_release",
    "catch": "catch",
    "handoff": "handoff",
}
Reason = Literal[
    "actor_binding_missing_or_protocol_unknown",
    "actor_binding_source_clip_or_window_mismatch",
    "actor_binding_capture_incomplete",
    "actor_binding_observations_missing_or_changed",
    "actor_binding_observations_ambiguous",
    "actor_reference_container_invalid",
    "event_timebase_unknown",
    "interval_timebase_or_bounds_invalid",
]


class ReplayIssue(BaseModel):
    event_id: uuid.UUID
    reason: Reason
    review_required: Literal[True] = True
    actor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    fields: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=30)


class RemovalReplayIssue(BaseModel):
    event_id: uuid.UUID
    reason: Literal["removed_event_identity_unresolved"]


class ReplayReview(BaseModel):
    job_id: uuid.UUID
    annotation_revision: int = Field(ge=0)
    issues: list[ReplayIssue] = Field(max_length=10000)
    removal_issues: list[RemovalReplayIssue] = Field(default_factory=list, max_length=10000)


@router.post("/api/v1/clips/{clip_id}/actor-binding-review")
async def publish_review(
    clip_id: uuid.UUID,
    body: ReplayReview,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    actor: Annotated[User, Depends(require_any_staff)],
    worker_job: Annotated[str | None, Header(alias="X-Job-ID")] = None,
) -> dict[str, bool]:
    if worker_job != str(body.job_id):
        raise HTTPException(403, "Review publication requires the current processor lease")
    clip = (
        await db.execute(select(Clip).where(Clip.id == clip_id).with_for_update())
    ).scalar_one_or_none()
    if clip is None:
        raise HTTPException(404, "Clip not found")
    require_active_clip(clip)
    job = await db.get(ProcessingJob, body.job_id)
    if (
        job is None
        or job.video_id != clip.video_id
        or job.job_type != JobType.pipeline
        or job.status != JobStatus.running
    ):
        raise HTTPException(409, "Review job does not own this recording")
    runtime = await db.get(WorkerRuntime, job.leased_by) if job.leased_by else None
    if runtime is None or runtime.registered_by != actor.id:
        raise HTTPException(403, "Review publication requires the registered processor account")
    latest_id = (
        await db.execute(
            select(ProcessingJob.id)
            .where(
                ProcessingJob.video_id == clip.video_id, ProcessingJob.job_type == JobType.pipeline
            )
            .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_id != job.id:
        raise HTTPException(409, "A newer processor job owns current review diagnostics")
    if annotation_revision(clip) != body.annotation_revision:
        raise HTTPException(
            409, "Annotations changed; recompute review against the current revision"
        )
    ids = {issue.event_id for issue in body.issues} | {
        issue.event_id for issue in body.removal_issues
    }
    if ids:
        owned = set(
            (
                await db.execute(
                    select(Event.id).where(Event.clip_id == clip.id, Event.id.in_(ids))
                )
            ).scalars()
        )
        if owned != ids:
            raise HTTPException(422, "Review includes an event outside this clip")
    removed_ids = {issue.event_id for issue in body.removal_issues}
    if removed_ids:
        removed = (await db.execute(select(Event).where(Event.id.in_(removed_ids)))).scalars().all()
        if any(
            (event.provenance or {}).get("state") != "superseded"
            or not (event.provenance or {}).get("removal")
            for event in removed
        ):
            raise HTTPException(422, "Removal review requires a preserved staff removal")
    key = f"actor-binding-review:{clip.id}"
    record = await db.get(SystemSetting, key)
    value = body.model_dump(mode="json")
    if record is None:
        db.add(SystemSetting(key=key, value=value, updated_by=actor.id))
    else:
        record.value, record.updated_by = value, actor.id
    await db.flush()
    return {"saved": True}


@router.get("/api/v1/clips/{clip_id}/actor-binding-review")
async def read_review(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _actor: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    clip = await db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "Clip not found")
    revision = annotation_revision(clip)
    events = effective_events(
        (
            await db.execute(
                select(Event)
                .where(Event.clip_id == clip.id)
                .order_by(Event.timestamp_seconds, Event.id)
            )
        )
        .scalars()
        .all()
    )
    events = [
        e
        for e in events
        if (e.provenance or {}).get("source") in {"human", "manual"}
        or (e.attributes or {}).get("source") in {"human", "manual"}
    ]
    sidecar = await db.get(SystemSetting, f"actor-binding-review:{clip.id}")
    stored = sidecar.value if sidecar is not None and isinstance(sidecar.value, dict) else {}
    latest = (
        await db.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.video_id == clip.video_id, ProcessingJob.job_type == JobType.pipeline
            )
            .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    replay = (
        stored.get("issues", [])
        if (
            stored.get("annotation_revision") == revision
            and latest is not None
            and stored.get("job_id") == str(latest.id)
            and latest.status in {JobStatus.running, JobStatus.succeeded}
        )
        else []
    )
    by_event: dict[str, list[dict[str, Any]]] = {}
    for issue in replay:
        by_event.setdefault(str(issue.get("event_id")), []).append(issue)
    removed_observations = []
    if replay or (
        stored.get("annotation_revision") == revision
        and latest is not None
        and stored.get("job_id") == str(latest.id)
        and latest.status in {JobStatus.running, JobStatus.succeeded}
    ):
        removal_issues = stored.get("removal_issues") or []
        removal_ids = [uuid.UUID(issue["event_id"]) for issue in removal_issues]
        if removal_ids:
            removed = (
                (
                    await db.execute(
                        select(Event).where(
                            Event.clip_id == clip.id,
                            Event.id.in_(removal_ids),
                            Event.provenance["state"].astext == "superseded",
                        )
                    )
                )
                .scalars()
                .all()
            )
            removed_observations = [
                {
                    "event": EventResponse.from_orm_event(event).model_dump(mode="json"),
                    "issues": [
                        issue for issue in removal_issues if issue["event_id"] == str(event.id)
                    ],
                }
                for event in removed
            ]
    observations = []
    clock_ids: dict[str, list[str]] = {}
    for event in events:
        family = PREDICTION_CLOCK_FAMILIES.get(event.event_type)
        if family:
            clock_ids.setdefault(family, []).append(str(event.id))
    for event in events:
        binding = (event.provenance or {}).get("actor_bindings")
        binding = binding if isinstance(binding, dict) else {}
        capture = binding.get("review_required")
        capture = list(capture) if isinstance(capture, list) else []
        if actor_references(event.attributes or {}):
            if binding.get("protocol") != PROTOCOL:
                capture.append({"reason": "actor_binding_missing_or_protocol_unknown"})
            else:
                scope = binding.get("scope")
                scope = scope if isinstance(scope, dict) else {}
                current_scope = {
                    "clip_id": str(clip.id),
                    "video_id": str(clip.video_id),
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                }
                if any(scope.get(key) != value for key, value in current_scope.items()):
                    capture.append({"reason": "actor_binding_source_clip_or_window_mismatch"})
        issues = capture + by_event.get(str(event.id), [])
        family = PREDICTION_CLOCK_FAMILIES.get(event.event_type)
        if family and len(set(clock_ids[family])) > 1:
            issues.append(
                {
                    "reason": "prediction_clock_ambiguous",
                    "review_required": True,
                    "clock_family": family,
                    "event_ids": clock_ids[family],
                }
            )
        if issues:
            observations.append(
                {
                    "event": EventResponse.from_orm_event(event).model_dump(mode="json"),
                    "issues": issues,
                }
            )
    return {
        "annotation_revision": revision,
        "observations": observations,
        "removed_observations": removed_observations,
    }
