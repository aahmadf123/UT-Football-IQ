"""Tests for the clips router (Issue #98)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user, require_coach_or_above
from app.main import app
from app.models import (
    Clip,
    SessionKind,
    SideOfBall,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from fastapi.testclient import TestClient


def _make_user() -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = UserRole.coach
    u.is_active = True
    return u


def _make_video(
    *,
    video_id: uuid.UUID | None = None,
    session_kind: SessionKind | None = None,
    our_possession: SideOfBall | None = None,
    opponent_team: str | None = None,
) -> Video:
    v = MagicMock(spec=Video)
    v.id = video_id or uuid.uuid4()
    v.filename = "t.mp4"
    v.storage_uri = "r2://x/t.mp4"
    v.status = VideoStatus.ready
    v.session_kind = session_kind
    v.our_possession = our_possession
    v.opponent_team = opponent_team
    v.recorded_at = datetime.now(UTC)
    v.created_at = datetime.now(UTC)
    # Capture-regime metadata (Issue #126) defaults to NULL on a fresh
    # video so MagicMock doesn't auto-fabricate a value that would later
    # fail pydantic validation in ClipResponse.
    v.capture_regime = None
    v.regime_confidence = None
    return v


def _mock_db_for_clip_create(video: Video, captured: list[Clip]):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            # First execute: parent-video lookup by id.
            result = MagicMock()
            result.scalar_one_or_none.return_value = video
            return result

        async def _flush() -> None:
            for c in captured:
                if getattr(c, "created_at", None) is None:
                    c.created_at = datetime.now(UTC)
                # SQLAlchemy Python-side defaults run on Insert; the mocked
                # flush has to stand in for them so the response can serialize.
                if getattr(c, "is_reviewed", None) is None:
                    c.is_reviewed = False

        session.execute = AsyncMock(side_effect=_execute)
        session.add = MagicMock(side_effect=captured.append)
        session.flush = AsyncMock(side_effect=_flush)
        yield session

    return _db


def _override(video: Video, captured: list[Clip]) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_for_clip_create(video, captured)


def _post_clip(video_id: uuid.UUID, body: dict[str, Any]) -> Any:
    with TestClient(app) as c:
        return c.post(f"/api/v1/videos/{video_id}/clips", json=body)


# ── our_possession + side_of_ball persistence ─────────────────────────────────


def test_create_clip_persists_our_possession_and_side_of_ball() -> None:
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.practice, our_possession=SideOfBall.offense)
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {
                "start_time": 0.0,
                "end_time": 5.0,
                "our_possession": "offense",
                "side_of_ball": "offense",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["our_possession"] == "offense"
    assert body["side_of_ball"] == "offense"
    persisted = captured[0]
    assert persisted.our_possession == SideOfBall.offense
    assert persisted.side_of_ball == SideOfBall.offense


# ── session_kind denormalization from parent video ────────────────────────────


def test_create_clip_denormalizes_session_kind_from_video() -> None:
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.game, opponent_team="Ohio")
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {
                "start_time": 0.0,
                "end_time": 5.0,
                "our_possession": "defense",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    assert resp.json()["session_kind"] == "game"
    assert captured[0].session_kind == SessionKind.game


# ── label_data promotion ──────────────────────────────────────────────────────


def test_create_clip_promotes_label_data_side_of_ball() -> None:
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.practice, our_possession=SideOfBall.defense)
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {
                "start_time": 0.0,
                "end_time": 5.0,
                "label_data": {"side_of_ball": "defense", "formation": "nickel"},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["side_of_ball"] == "defense"
    # our_possession defaults to side_of_ball when not supplied.
    assert body["our_possession"] == "defense"
    # The JSON value is preserved (non-destructive promotion).
    assert body["label_data"]["side_of_ball"] == "defense"


def test_create_clip_ignores_unknown_label_data_side_of_ball() -> None:
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.practice, our_possession=SideOfBall.offense)
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {
                "start_time": 0.0,
                "end_time": 5.0,
                "label_data": {"side_of_ball": "kickoff_return"},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    # Garbage in label_data is not promoted; column stays null.
    assert resp.json()["side_of_ball"] is None


# ── Game-clip possession requirement ──────────────────────────────────────────


def test_create_game_clip_requires_our_possession() -> None:
    captured: list[Clip] = []
    # Game session with no session-level our_possession (possession flips).
    video = _make_video(session_kind=SessionKind.game, opponent_team="Ohio")
    _override(video, captured)
    try:
        resp = _post_clip(video.id, {"start_time": 0.0, "end_time": 5.0})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert "our_possession" in resp.text
    assert captured == []


def test_create_practice_clip_may_omit_our_possession() -> None:
    """Practice clips inherit our_possession from the parent video."""
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.practice, our_possession=SideOfBall.offense)
    _override(video, captured)
    try:
        resp = _post_clip(video.id, {"start_time": 0.0, "end_time": 5.0})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    # No clip-level value; the session-level value is the source of truth.
    body = resp.json()
    assert body["our_possession"] is None
    assert body["side_of_ball"] is None


# ── play_call_id round-trip (Issue #150 cross-regime pairing) ─────────────────


def test_create_clip_persists_play_call_id() -> None:
    """A coach-tagged play-call code is persisted and returned on create."""
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.game, our_possession=SideOfBall.offense)
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {
                "start_time": 0.0,
                "end_time": 5.0,
                "our_possession": "offense",
                "play_call_id": "Gun-Trips-Rt-22-Z",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    assert resp.json()["play_call_id"] == "Gun-Trips-Rt-22-Z"
    assert captured[0].play_call_id == "Gun-Trips-Rt-22-Z"


def test_create_clip_defaults_play_call_id_to_none() -> None:
    captured: list[Clip] = []
    video = _make_video(session_kind=SessionKind.practice, our_possession=SideOfBall.offense)
    _override(video, captured)
    try:
        resp = _post_clip(video.id, {"start_time": 0.0, "end_time": 5.0})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    assert resp.json()["play_call_id"] is None


def test_update_clip_sets_play_call_id() -> None:
    """Coaches tag matched practice<->game plays by PATCHing play_call_id."""
    clip = Clip(id=uuid.uuid4(), video_id=uuid.uuid4(), start_time=0.0, end_time=5.0)
    clip.is_reviewed = False
    clip.uncertainty_calibrated = False
    clip.created_at = datetime.now(UTC)

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            result.scalar_one_or_none.return_value = clip
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[require_coach_or_above] = lambda: _make_user()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.patch(f"/api/v1/clips/{clip.id}", json={"play_call_id": "PLAY-Z"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["play_call_id"] == "PLAY-Z"
    assert clip.play_call_id == "PLAY-Z"
