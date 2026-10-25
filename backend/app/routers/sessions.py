"""Persisted session workspaces with independent recordings and manual play matching."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff
from app.models import Clip, FootballSession, Play, PlayView, SessionKind, User, Video
from app.routers.videos import VideoResponse
from app.services.annotations import touch_annotation
from app.services.clip_state import active_clip, clip_is_active, lock_boundary_scope

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    session_kind: SessionKind | None = None
    recorded_at: datetime | None = None
    opponent_team: str | None = Field(default=None, max_length=120)
    library_role: Literal["toledo", "opponent"] = "toledo"
    notes: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def internal_practice(self) -> "SessionCreate":
        if self.session_kind in {SessionKind.practice, SessionKind.scrimmage}:
            self.opponent_team = None
        return self

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Session title cannot be empty")
        return value


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    session_kind: SessionKind | None = None
    recorded_at: datetime | None = None
    opponent_team: str | None = Field(default=None, max_length=120)
    library_role: Literal["toledo", "opponent"] | None = None
    notes: str | None = Field(default=None, max_length=10000)

    @field_validator("title", "library_role")
    @classmethod
    def required_if_present(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("Title and library ownership cannot be empty")
        return value.strip()


class RecordingAssociation(BaseModel):
    video_id: uuid.UUID
    camera_label: str | None = Field(default=None, max_length=120)


class PlayMatch(BaseModel):
    clip_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    play_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=255)


async def _get_session(db: AsyncSession, session_id: uuid.UUID) -> FootballSession:
    row = (
        await db.execute(select(FootballSession).where(FootballSession.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Session not found")
    return row


async def ensure_recording_plays(db: AsyncSession, video: Video) -> None:
    """Group generated angle clips by play call; leave other views independent."""
    if video.practice_session_id is None:
        return
    clips = (await db.execute(select(Clip).where(Clip.video_id == video.id))).scalars().all()
    if not clips:
        return
    views = (
        (await db.execute(select(PlayView).where(PlayView.clip_id.in_([c.id for c in clips]))))
        .scalars()
        .all()
    )
    by_clip = {view.clip_id: view for view in views}
    groups: dict[str, list[Clip]] = {}
    for clip in clips:
        if clip.play_call_id:
            groups.setdefault(clip.play_call_id, []).append(clip)
    grouped_ids: set[uuid.UUID] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        group_views = [by_clip[clip.id] for clip in group if clip.id in by_clip]
        if any(view.match_source != "independent" for view in group_views):
            continue
        existing_play_ids = list(dict.fromkeys(view.play_id for view in group_views))
        if existing_play_ids:
            play_id = existing_play_ids[0]
        else:
            play = Play(id=uuid.uuid4(), session_id=video.practice_session_id)
            db.add(play)
            await db.flush()
            play_id = play.id
        for clip in group:
            view = by_clip.get(clip.id)
            if view is None:
                db.add(
                    PlayView(
                        id=uuid.uuid4(),
                        play_id=play_id,
                        clip_id=clip.id,
                        match_source="independent",
                    )
                )
            else:
                view.play_id = play_id
            grouped_ids.add(clip.id)
        await db.flush()
        for old_play_id in existing_play_ids[1:]:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(PlayView)
                    .where(PlayView.play_id == old_play_id)
                )
            ).scalar_one()
            if not count:
                old_play = await db.get(Play, old_play_id)
                if old_play is not None:
                    await db.delete(old_play)
    for clip in clips:
        if clip.id in grouped_ids or clip.id in by_clip:
            continue
        play = Play(id=uuid.uuid4(), session_id=video.practice_session_id)
        db.add(play)
        await db.flush()
        db.add(
            PlayView(id=uuid.uuid4(), play_id=play.id, clip_id=clip.id, match_source="independent")
        )
    await db.flush()


def _allows_same_recording_angles(clips: Sequence[Row[tuple[Clip, Video]]]) -> bool:
    by_video: dict[uuid.UUID, list[Clip]] = {}
    for clip, video in clips:
        by_video.setdefault(video.id, []).append(clip)
    for group in by_video.values():
        if len(group) < 2:
            continue
        play_call_ids = {clip.play_call_id for clip in group}
        angles = [(clip.label_data or {}).get("view") for clip in group]
        if (
            len(play_call_ids) != 1
            or not next(iter(play_call_ids))
            or any(not angle for angle in angles)
            or len(set(angles)) != len(angles)
        ):
            return False
    return True


async def session_response(db: AsyncSession, session: FootballSession) -> dict[str, Any]:
    recordings = (
        (
            await db.execute(
                select(Video)
                .where(Video.practice_session_id == session.id)
                .order_by(Video.created_at)
            )
        )
        .scalars()
        .all()
    )
    plays = (
        (
            await db.execute(
                select(Play)
                .where(Play.session_id == session.id)
                .order_by(Play.sequence.asc().nullslast(), Play.created_at)
            )
        )
        .scalars()
        .all()
    )
    view_rows = (
        (
            await db.execute(
                select(PlayView, Clip, Video)
                .join(Clip, Clip.id == PlayView.clip_id)
                .join(Video, Video.id == Clip.video_id)
                .where(PlayView.play_id.in_([p.id for p in plays]))
                .where(active_clip())
            )
        ).all()
        if plays
        else []
    )
    views: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for view, clip, video in view_rows:
        views.setdefault(view.play_id, []).append(
            {
                "id": str(view.id),
                "clip_id": str(clip.id),
                "video_id": str(video.id),
                "camera_label": video.camera_label,
                "alignment_offset_seconds": view.alignment_offset_seconds,
                "alignment_confidence": view.alignment_confidence,
                "match_source": view.match_source,
            }
        )
    plays = [play for play in plays if play.id in views]
    countable = {
        view.play_id
        for view, clip, _video in view_rows
        if (clip.label_data or {}).get("canonical_count_eligible", True) is True
    }
    return {
        "id": str(session.id),
        "title": session.title,
        "session_kind": session.session_kind,
        "recorded_at": session.recorded_at,
        "opponent_team": session.opponent_team,
        "library_role": session.library_role,
        "notes": session.notes,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "recording_count": len(recordings),
        "play_count": sum(play.id in countable for play in plays),
        "source_window_count": sum(play.id not in countable for play in plays),
        "recordings": [VideoResponse.from_orm_video(v).model_dump(mode="json") for v in recordings],
        "plays": [
            {
                "id": str(p.id),
                "session_id": str(p.session_id),
                "title": p.title,
                "sequence": p.sequence,
                "count_eligible": p.id in countable,
                "views": views.get(p.id, []),
            }
            for p in plays
        ],
    }


@router.get("")
async def list_sessions(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    library_role: Literal["toledo", "opponent"] | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(FootballSession)
        .order_by(
            FootballSession.recorded_at.desc().nullslast(),
            FootballSession.created_at.desc(),
            FootballSession.id,
        )
        .offset(offset)
        .limit(limit)
    )
    if library_role:
        stmt = stmt.where(FootballSession.library_role == library_role)
    rows = (await db.execute(stmt)).scalars().all()
    return [await session_response(db, row) for row in rows]


@router.post("", status_code=201)
async def create_session(
    body: SessionCreate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    row = FootballSession(id=uuid.uuid4(), created_by=user.id, **body.model_dump())
    db.add(row)
    await db.flush()
    return await session_response(db, row)


@router.get("/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return await session_response(db, await _get_session(db, session_id))


@router.patch("/{session_id}")
async def update_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    await lock_boundary_scope(db, session_id)
    row = await _get_session(db, session_id)
    before_context = (row.session_kind, row.opponent_team, row.library_role)
    row.updated_at = datetime.now(UTC)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    if row.session_kind in {SessionKind.practice, SessionKind.scrimmage}:
        row.opponent_team = None
    if before_context != (row.session_kind, row.opponent_team, row.library_role):
        values: dict[str, Any] = {"library_role": row.library_role}
        if row.session_kind is not None:
            values.update(session_kind=row.session_kind, opponent_team=row.opponent_team)
        await db.execute(
            update(Video).where(Video.practice_session_id == session_id).values(**values)
        )
        clips = (
            (
                await db.execute(
                    select(Clip)
                    .where(
                        Clip.video_id.in_(
                            select(Video.id).where(Video.practice_session_id == session_id)
                        )
                    )
                    .order_by(Clip.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for clip in clips:
            if row.session_kind is not None:
                clip.session_kind = row.session_kind
            touch_annotation(clip)
    await db.flush()
    return await session_response(db, row)


@router.post("/{session_id}/recordings")
async def associate_recording(
    session_id: uuid.UUID,
    body: RecordingAssociation,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    await lock_boundary_scope(db, session_id)
    row = await _get_session(db, session_id)
    video = (
        await db.execute(select(Video).where(Video.id == body.video_id).with_for_update())
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(404, "Recording not found")
    row.updated_at = datetime.now(UTC)
    old_session = video.practice_session_id
    old_context = (video.session_kind, video.opponent_team, video.library_role)
    video.practice_session_id = row.id
    video.library_role = row.library_role
    if row.session_kind is not None:
        video.session_kind = row.session_kind
        video.opponent_team = (
            None
            if row.session_kind in {SessionKind.practice, SessionKind.scrimmage}
            else row.opponent_team
        )
    if body.camera_label is not None:
        video.camera_label = body.camera_label
    # Moving a recording must not silently move a multi-recording canonical play.
    moving = old_session is not None and old_session != row.id
    if moving or old_context != (video.session_kind, video.opponent_team, video.library_role):
        moved_clips = (
            (
                await db.execute(
                    select(Clip)
                    .where(Clip.video_id == video.id)
                    .order_by(Clip.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for clip in moved_clips:
            if row.session_kind is not None:
                clip.session_kind = row.session_kind
            touch_annotation(clip)
    if moving:
        existing = (
            (
                await db.execute(
                    select(PlayView)
                    .join(Clip, Clip.id == PlayView.clip_id)
                    .where(Clip.video_id == video.id)
                )
            )
            .scalars()
            .all()
        )
        for view in existing:
            await db.delete(view)
    await ensure_recording_plays(db, video)
    await db.flush()
    return await session_response(db, row)


@router.post("/{session_id}/plays/match")
async def match_play(
    session_id: uuid.UUID,
    body: PlayMatch,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _user: Annotated[User, Depends(require_any_staff)],
) -> dict[str, Any]:
    row = await _get_session(db, session_id)
    # Serialize matching and concurrent association changes within a workspace.
    await lock_boundary_scope(db, session_id)
    await db.execute(
        select(FootballSession.id).where(FootballSession.id == session_id).with_for_update()
    )
    row.updated_at = datetime.now(UTC)
    ids = list(dict.fromkeys(body.clip_ids))
    clips = (
        await db.execute(
            select(Clip, Video)
            .join(Video, Video.id == Clip.video_id)
            .where(Clip.id.in_(ids))
            .order_by(Clip.id)
            .with_for_update(of=Clip)
        )
    ).all()
    if len(clips) != len(ids):
        raise HTTPException(404, "One or more clips were not found")
    if any(not clip_is_active(clip) for clip, _video in clips):
        raise HTTPException(409, "A selected clip was replaced; match its active replacement")
    if any(video.practice_session_id != session_id for _, video in clips):
        raise HTTPException(409, "All recordings must belong to this session")
    if not _allows_same_recording_angles(clips):
        raise HTTPException(409, "Same-recording views must be distinct angles of one play")
    play = (
        (
            await db.execute(
                select(Play).where(Play.id == body.play_id, Play.session_id == session_id)
            )
        ).scalar_one_or_none()
        if body.play_id
        else None
    )
    if body.play_id and play is None:
        raise HTTPException(404, "Play not found in this session")
    if play is None:
        play = Play(id=uuid.uuid4(), session_id=session_id, title=body.title)
        db.add(play)
        await db.flush()
    elif body.title is not None:
        play.title = body.title
    current = (await db.execute(select(PlayView).where(PlayView.clip_id.in_(ids)))).scalars().all()
    by_clip = {v.clip_id: v for v in current}
    old_play_ids = {v.play_id for v in current if v.play_id != play.id}
    existing_clips = (
        await db.execute(
            select(Clip, Video)
            .join(PlayView, PlayView.clip_id == Clip.id)
            .join(Video, Video.id == Clip.video_id)
            .where(PlayView.play_id == play.id, ~Clip.id.in_(ids))
            .order_by(Clip.id)
            .with_for_update(of=Clip)
        )
    ).all()
    if existing_clips and not any(clip_is_active(clip) for clip, _video in existing_clips):
        raise HTTPException(409, "This canonical play is historical; match active replacements")
    active_existing_clips = [row for row in existing_clips if clip_is_active(row[0])]
    if not _allows_same_recording_angles([*clips, *active_existing_clips]):
        raise HTTPException(409, "This play already has a conflicting view from that recording")
    for clip, _video in clips:
        clip.label_data = {
            **(clip.label_data or {}),
            "canonical_count_eligible": True,
            "logical_play_status": "staff_matched",
        }
        touch_annotation(clip)
    for clip, _video in active_existing_clips:
        touch_annotation(clip)
    for clip_id in ids:
        if clip_id in by_clip:
            by_clip[clip_id].play_id = play.id
            by_clip[clip_id].match_source = "manual"
        else:
            db.add(
                PlayView(id=uuid.uuid4(), play_id=play.id, clip_id=clip_id, match_source="manual")
            )
    await db.flush()
    for old_id in old_play_ids:
        count = (
            await db.execute(
                select(func.count()).select_from(PlayView).where(PlayView.play_id == old_id)
            )
        ).scalar_one()
        if not count:
            old_play = await db.get(Play, old_id)
            if old_play:
                await db.delete(old_play)
    await db.flush()
    return await session_response(db, row)
