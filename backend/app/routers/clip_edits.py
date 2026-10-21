"""Audited source-window editing, preserving originals and later corrections."""

import copy
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_coach_or_above
from app.models import Clip, ClipEdit, CoachCorrection, CorrectionType, Play, PlayView, User, Video
from app.routers.clips import ClipResponse, attach_play_context
from app.services.annotations import annotation_revision, check_revision, touch_annotation
from app.services.clip_state import (
    active_clip,
    clip_is_active,
    layout_is_protected,
    lock_boundary_scope,
)

router = APIRouter(tags=["clips"])


class SplitClip(BaseModel):
    at_seconds: float = Field(ge=0, allow_inf_nan=False)
    expected_revision: int = Field(ge=0)


class MergeClips(BaseModel):
    clip_ids: list[uuid.UUID] = Field(min_length=2, max_length=10)
    expected_revisions: dict[str, int]


class UndoEdit(BaseModel):
    expected_revisions: dict[str, int]


def operation_data(operation: ClipEdit) -> dict[str, Any]:
    return {
        "operation_id": str(operation.id),
        "operation_type": operation.operation_type,
        "original_clip_ids": operation.original_clip_ids,
        "result_clip_ids": operation.result_clip_ids,
        "revisions": operation.after_revisions,
        "undo_available": operation.state == "active",
        "state": operation.state,
        "created_at": operation.created_at.isoformat(),
        "actor_id": str(operation.actor_id) if operation.actor_id else None,
        "undone_at": operation.undone_at.isoformat() if operation.undone_at else None,
        "before": {
            clip_id: {
                "start_seconds": operation.before_state[clip_id].get("start_time"),
                "end_seconds": operation.before_state[clip_id].get("end_time"),
                "revision": (operation.before_state[clip_id].get("label_data") or {}).get(
                    "_annotation_revision", 0
                ),
            }
            for clip_id in operation.original_clip_ids
        },
        "after": operation.before_state.get("_result_windows", {}),
    }


async def locked_clips(db: AsyncSession, ids: list[uuid.UUID]) -> list[Clip]:
    scopes = (
        await db.execute(
            select(Video.practice_session_id, Video.id)
            .join(Clip, Clip.video_id == Video.id)
            .where(Clip.id.in_(ids))
        )
    ).all()
    for scope_id in sorted({session_id or video_id for session_id, video_id in scopes}, key=str):
        await lock_boundary_scope(db, scope_id)
    rows = list(
        (await db.execute(select(Clip).where(Clip.id.in_(ids)).order_by(Clip.id).with_for_update()))
        .scalars()
        .all()
    )
    if len(rows) != len(set(ids)):
        raise HTTPException(404, "Clip not found")
    return sorted(rows, key=lambda clip: (clip.start_time, str(clip.id)))


async def require_independent_views(db: AsyncSession, clips: list[Clip]) -> None:
    ids = [clip.id for clip in clips]
    play_ids = (
        (await db.execute(select(PlayView.play_id).where(PlayView.clip_id.in_(ids))))
        .scalars()
        .all()
    )
    if play_ids:
        other = (
            await db.execute(
                select(PlayView.id)
                .join(Clip, Clip.id == PlayView.clip_id)
                .where(PlayView.play_id.in_(play_ids), ~PlayView.clip_id.in_(ids), active_clip())
                .limit(1)
            )
        ).scalar_one_or_none()
        if other is not None:
            raise HTTPException(
                409, "Unmatch alternate camera views before changing this play's boundaries"
            )


def audit(db: AsyncSession, clip: Clip, operation: ClipEdit, user: User, action: str) -> None:
    db.add(
        CoachCorrection(
            id=uuid.uuid4(),
            clip_id=clip.id,
            correction_type=CorrectionType.clip_boundary,
            corrected_by=user.id,
            original_value={"start_seconds": clip.start_time, "end_seconds": clip.end_time},
            corrected_value={
                "operation": action,
                "clip_edit_operation_id": str(operation.id),
                "original_clip_ids": operation.original_clip_ids,
                "result_clip_ids": operation.result_clip_ids,
                "_revision": annotation_revision(clip),
            },
            training_eligible=False,
            notes="Structural clip edit; originals retained.",
        )
    )


async def create_edit(
    db: AsyncSession,
    user: User,
    clips: list[Clip],
    windows: list[tuple[float, float]],
    operation_type: str,
) -> dict[str, Any]:
    if any(not clip_is_active(clip) for clip in clips):
        raise HTTPException(409, "This clip was replaced; open an active replacement")
    await require_independent_views(db, clips)
    video = await db.get(Video, clips[0].video_id)
    if video is None:
        raise HTTPException(404, "Video not found")
    await db.execute(select(Video.id).where(Video.id == video.id).with_for_update())
    operation = ClipEdit(
        id=uuid.uuid4(),
        video_id=video.id,
        actor_id=user.id,
        operation_type=operation_type,
        state="active",
        original_clip_ids=[str(clip.id) for clip in clips],
        result_clip_ids=[],
        before_state={
            str(clip.id): {
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "label_data": copy.deepcopy(clip.label_data),
                "provenance": copy.deepcopy(clip.provenance),
            }
            for clip in clips
        },
        after_revisions={},
    )
    db.add(operation)
    results = []
    views = {
        (clip.label_data or {}).get("view")
        if isinstance((clip.label_data or {}).get("view"), str)
        else None
        for clip in clips
    }
    regimes = {clip.capture_regime for clip in clips}
    shared_view = next(iter(views)) if len(views) == 1 else None
    shared_regime = next(iter(regimes)) if len(regimes) == 1 else None
    for start, end in windows:
        result = Clip(
            id=uuid.uuid4(),
            video_id=video.id,
            start_time=start,
            end_time=end,
            session_kind=video.session_kind,
            capture_regime=shared_regime,
            regime_confidence=min(clip.regime_confidence or 0 for clip in clips)
            if shared_regime
            else None,
            boundary_source="manual",
            boundary_confidence=1.0,
            is_reviewed=True,
            reviewed_by=user.id,
            label_data={
                "_annotation_revision": 1,
                "canonical_count_eligible": True,
                "logical_play_status": f"staff_{operation_type}",
                **({"view": shared_view} if shared_view else {}),
            },
            provenance={
                "source": "manual",
                "validated": False,
                "clip_edit_operation_id": str(operation.id),
                "derived_from_clip_ids": operation.original_clip_ids,
                "artifact_state": "source_recording",
                "analysis_state": "reprocessing_required",
            },
        )
        db.add(result)
        results.append(result)
        if video.practice_session_id:
            play = Play(
                id=uuid.uuid4(),
                session_id=video.practice_session_id,
                metadata_={
                    "clip_edit_operation_id": str(operation.id),
                    "derived_from_clip_ids": operation.original_clip_ids,
                },
            )
            db.add(play)
            await db.flush()
            db.add(
                PlayView(id=uuid.uuid4(), play_id=play.id, clip_id=result.id, match_source="manual")
            )
    operation.result_clip_ids = [str(clip.id) for clip in results]
    operation.before_state = {
        **operation.before_state,
        "_result_windows": {
            str(clip.id): {
                "start_seconds": clip.start_time,
                "end_seconds": clip.end_time,
                "revision": annotation_revision(clip),
            }
            for clip in results
        },
    }
    for clip in clips:
        clip.provenance = {
            **(clip.provenance or {}),
            "state": "superseded",
            "clip_edit_operation_id": str(operation.id),
            "replacement_clip_ids": operation.result_clip_ids,
        }
        clip.label_data = {**(clip.label_data or {}), "canonical_count_eligible": False}
        touch_annotation(clip)
        audit(db, clip, operation, user, operation_type)
    operation.after_revisions = {
        str(clip.id): annotation_revision(clip) for clip in [*clips, *results]
    }
    await db.flush()
    responses = await attach_play_context(
        db, [ClipResponse.from_orm_clip(clip) for clip in results]
    )
    return {
        **operation_data(operation),
        "clips": [clip.model_dump(mode="json") for clip in responses],
        "superseded_clip_ids": operation.original_clip_ids,
    }


@router.post("/api/v1/clips/{clip_id}/split")
async def split_clip(
    clip_id: uuid.UUID,
    body: SplitClip,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    clips = await locked_clips(db, [clip_id])
    clip = clips[0]
    check_revision(clip, body.expected_revision)
    if not clip.start_time + 0.1 <= body.at_seconds <= clip.end_time - 0.1:
        raise HTTPException(422, "Split must retain at least 0.1 seconds on each side")
    return await create_edit(
        db,
        user,
        clips,
        [(clip.start_time, body.at_seconds), (body.at_seconds, clip.end_time)],
        "split",
    )


@router.post("/api/v1/clips/merge")
async def merge_clips(
    body: MergeClips,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    if len(set(body.clip_ids)) != len(body.clip_ids):
        raise HTTPException(422, "Select each clip once")
    clips = await locked_clips(db, body.clip_ids)
    if len({clip.video_id for clip in clips}) != 1:
        raise HTTPException(422, "Merge clips from one recording")
    for clip in clips:
        if str(clip.id) not in body.expected_revisions:
            raise HTTPException(422, "Provide every selected clip's revision")
        check_revision(clip, body.expected_revisions[str(clip.id)])
    active = list(
        (
            await db.execute(
                select(Clip)
                .where(Clip.video_id == clips[0].video_id, active_clip())
                .order_by(Clip.start_time, Clip.id)
            )
        )
        .scalars()
        .all()
    )
    positions = [index for index, clip in enumerate(active) if clip.id in body.clip_ids]
    if not positions or positions != list(range(min(positions), max(positions) + 1)):
        raise HTTPException(409, "Select consecutive clips without skipping an intervening play")
    if any(
        right.start_time - left.end_time > 30 for left, right in zip(clips, clips[1:], strict=False)
    ):
        raise HTTPException(422, "Selected clips have more than 30 seconds between them")
    return await create_edit(
        db,
        user,
        clips,
        [(min(clip.start_time for clip in clips), max(clip.end_time for clip in clips))],
        "merge",
    )


@router.get("/api/v1/clips/{clip_id}/edits")
async def list_clip_edits(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_coach_or_above)],
) -> list[dict[str, Any]]:
    clip = await db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "Clip not found")
    operations = (
        (
            await db.execute(
                select(ClipEdit)
                .where(ClipEdit.video_id == clip.video_id)
                .order_by(ClipEdit.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    relevant = [
        operation
        for operation in operations
        if str(clip_id) in [*operation.original_clip_ids, *operation.result_clip_ids]
    ]
    ids = {uuid.UUID(key) for operation in relevant for key in operation.after_revisions}
    current = (
        {
            str(row.id): annotation_revision(row)
            for row in (await db.execute(select(Clip).where(Clip.id.in_(ids)))).scalars().all()
        }
        if ids
        else {}
    )
    response = []
    for operation in relevant:
        data = operation_data(operation)
        if operation.state == "active" and any(
            current.get(key) != revision for key, revision in operation.after_revisions.items()
        ):
            data["undo_available"] = False
            data["undo_blocked_reason"] = "A later correction changed one of these clips."
        response.append(data)
    return response


@router.post("/api/v1/clip-edits/{operation_id}/undo")
async def undo_clip_edit(
    operation_id: uuid.UUID,
    body: UndoEdit,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    await lock_boundary_scope(db, operation_id)
    operation = (
        await db.execute(select(ClipEdit).where(ClipEdit.id == operation_id).with_for_update())
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(404, "Clip edit not found")
    if operation.state != "active":
        raise HTTPException(409, "This edit has already been undone")
    clips = await locked_clips(db, [uuid.UUID(clip_id) for clip_id in operation.after_revisions])
    await require_independent_views(
        db, [clip for clip in clips if str(clip.id) in operation.result_clip_ids]
    )
    for clip in clips:
        key = str(clip.id)
        if key not in body.expected_revisions:
            raise HTTPException(422, "Provide the revision for every original and resulting clip")
        check_revision(clip, body.expected_revisions[key])
        if annotation_revision(clip) != operation.after_revisions[key]:
            raise HTTPException(
                409, "A later correction changed this edit; undo would overwrite it"
            )
    originals, results = [], []
    for clip in clips:
        revision = annotation_revision(clip) + 1
        if str(clip.id) in operation.original_clip_ids:
            before = operation.before_state[str(clip.id)]
            clip.label_data = {**(before["label_data"] or {}), "_annotation_revision": revision}
            clip.provenance = before["provenance"]
            originals.append(clip)
        else:
            clip.provenance = {
                **(clip.provenance or {}),
                "state": "superseded",
                "replacement_clip_ids": operation.original_clip_ids,
                "undone_edit_operation_id": str(operation.id),
            }
            clip.label_data = {
                **(clip.label_data or {}),
                "_annotation_revision": revision,
                "canonical_count_eligible": False,
            }
            results.append(clip)
        audit(db, clip, operation, user, "undo")
    operation.state, operation.undone_at, operation.undone_by = "undone", datetime.now(UTC), user.id
    # Undoing a descendant structural edit restores its exact before state.
    # Allow the parent edit's undo to proceed with these new monotonic revisions
    # while leaving any independent later correction as a conflict.
    parents = (
        (
            await db.execute(
                select(ClipEdit)
                .where(
                    ClipEdit.video_id == operation.video_id,
                    ClipEdit.state == "active",
                    ClipEdit.id != operation.id,
                )
                .order_by(ClipEdit.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for parent in parents:
        revisions = dict(parent.after_revisions)
        for clip in originals:
            key = str(clip.id)
            restored_revision = (operation.before_state[key].get("label_data") or {}).get(
                "_annotation_revision", 0
            )
            if key in revisions and revisions[key] == restored_revision:
                revisions[key] = annotation_revision(clip)
        parent.after_revisions = revisions
    await db.flush()
    responses = await attach_play_context(
        db, [ClipResponse.from_orm_clip(clip) for clip in originals]
    )
    return {
        **operation_data(operation),
        "clips": [clip.model_dump(mode="json") for clip in responses],
        "revisions": {str(clip.id): annotation_revision(clip) for clip in clips},
        "superseded_clip_ids": [str(clip.id) for clip in results],
    }


@router.get("/api/v1/videos/{video_id}/boundary-overrides")
async def boundary_overrides(
    video_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_coach_or_above)],
) -> dict[str, Any]:
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Video not found")
    active = (
        await db.execute(select(ClipEdit.id).where(ClipEdit.video_id == video_id).limit(1))
    ).scalar_one_or_none()
    clips = (
        (
            await db.execute(
                select(Clip)
                .where(Clip.video_id == video_id, active_clip())
                .order_by(Clip.start_time)
            )
        )
        .scalars()
        .all()
    )
    has_overrides = active is not None or await layout_is_protected(db, video_id, clips)
    return {
        "active": has_overrides,
        "clips": [ClipResponse.from_orm_clip(clip).model_dump(mode="json") for clip in clips]
        if has_overrides
        else [],
    }
