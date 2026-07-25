"""Tests for the same-session clip result-state surface (Issue #147).

Covers:
  - ``result_state`` persistence on clip create + the derived ``is_preliminary``
    / ``review_state`` fields in the response.
  - ``_derive_review_state`` precedence (reviewed > low_confidence > needs_review).
  - ``POST /api/v1/videos/{video_id}/clips/finalize`` — the nightly upgrade that
    flips ``preliminary`` clips to ``final``.

DB access is mocked (mirrors ``tests/test_clips.py``) so the suite needs no
Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    Clip,
    ClipResultState,
    SessionKind,
    SideOfBall,
    User,
    UserRole,
    Video,
    VideoStatus,
)
from app.routers.clips import _derive_review_state
from fastapi.testclient import TestClient


def _make_user() -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = UserRole.coach
    u.is_active = True
    return u


def _make_video(video_id: uuid.UUID | None = None) -> Video:
    v = MagicMock(spec=Video)
    v.id = video_id or uuid.uuid4()
    v.filename = "t.mp4"
    v.storage_uri = "r2://x/t.mp4"
    v.status = VideoStatus.ready
    v.session_kind = SessionKind.practice
    v.our_possession = SideOfBall.offense
    v.opponent_team = None
    v.recorded_at = datetime.now(UTC)
    v.created_at = datetime.now(UTC)
    v.capture_regime = None
    v.regime_confidence = None
    return v


def _mock_db_for_clip_create(video: Video, captured: list[Clip]):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(_stmt: Any) -> Any:
            result = MagicMock()
            result.scalar_one_or_none.return_value = video
            return result

        async def _flush() -> None:
            for c in captured:
                if getattr(c, "created_at", None) is None:
                    c.created_at = datetime.now(UTC)
                if getattr(c, "is_reviewed", None) is None:
                    c.is_reviewed = False
                if getattr(c, "uncertainty_calibrated", None) is None:
                    c.uncertainty_calibrated = False

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


# ── result_state persistence + derived fields on create ───────────────────────


def test_create_clip_persists_preliminary_result_state() -> None:
    captured: list[Clip] = []
    video = _make_video()
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {"start_time": 0.0, "end_time": 5.0, "result_state": "preliminary"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["result_state"] == "preliminary"
    assert body["is_preliminary"] is True
    # A fresh, confident-enough first-pass clip awaits review.
    assert body["review_state"] == "needs_review"
    assert captured[0].result_state == "preliminary"


def test_create_clip_defaults_result_state_to_null() -> None:
    captured: list[Clip] = []
    video = _make_video()
    _override(video, captured)
    try:
        resp = _post_clip(video.id, {"start_time": 0.0, "end_time": 5.0})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["result_state"] is None
    assert body["is_preliminary"] is False


def test_create_clip_low_confidence_review_state() -> None:
    captured: list[Clip] = []
    video = _make_video()
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {"start_time": 0.0, "end_time": 5.0, "confidence": 0.2, "result_state": "preliminary"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    assert resp.json()["review_state"] == "low_confidence"


def test_create_clip_rejects_unknown_result_state() -> None:
    captured: list[Clip] = []
    video = _make_video()
    _override(video, captured)
    try:
        resp = _post_clip(
            video.id,
            {"start_time": 0.0, "end_time": 5.0, "result_state": "bogus"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
    assert captured == []


# ── _derive_review_state precedence ───────────────────────────────────────────


def _clip(**attrs: Any) -> Clip:
    c = Clip()
    c.is_reviewed = attrs.get("is_reviewed", False)
    c.confidence = attrs.get("confidence", None)
    c.uncertainty_score = attrs.get("uncertainty_score", None)
    c.uncertainty_calibrated = attrs.get("uncertainty_calibrated", False)
    return c


def test_review_state_reviewed_beats_low_confidence() -> None:
    # A reviewed clip is "reviewed" even with poor confidence — the coach signed off.
    assert _derive_review_state(_clip(is_reviewed=True, confidence=0.01)) == "reviewed"


def test_review_state_low_confidence_from_confidence() -> None:
    assert _derive_review_state(_clip(confidence=0.3)) == "low_confidence"


def test_review_state_low_confidence_from_calibrated_uncertainty() -> None:
    # Calibrated high uncertainty (Issue #146) also counts as low confidence.
    assert (
        _derive_review_state(_clip(uncertainty_score=0.9, uncertainty_calibrated=True))
        == "low_confidence"
    )


def test_review_state_uncalibrated_uncertainty_is_ignored() -> None:
    # An uncalibrated uncertainty score must never be treated as trusted.
    assert (
        _derive_review_state(_clip(uncertainty_score=0.9, uncertainty_calibrated=False))
        == "needs_review"
    )


def test_review_state_confident_first_pass_needs_review() -> None:
    assert _derive_review_state(_clip(confidence=0.95)) == "needs_review"


# ── finalize endpoint (nightly upgrade) ───────────────────────────────────────


def _mock_db_for_finalize(video: Video | None, rowcount: int):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()

        async def _execute(stmt: Any) -> Any:
            result = MagicMock()
            text = str(stmt).lower()
            if text.startswith("update"):
                result.rowcount = rowcount
            else:
                result.scalar_one_or_none.return_value = video
            return result

        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        yield session

    return _db


def test_finalize_flips_preliminary_clips() -> None:
    video = _make_video()
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_for_finalize(video, rowcount=5)
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/v1/videos/{video.id}/clips/finalize")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["video_id"] == str(video.id)
    assert body["finalized_count"] == 5


def test_finalize_unknown_video_returns_404() -> None:
    missing = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_for_finalize(None, rowcount=0)
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/v1/videos/{missing}/clips/finalize")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_final_result_state_is_not_preliminary() -> None:
    assert ClipResultState.final.value == "final"
    assert ClipResultState.preliminary.value == "preliminary"
