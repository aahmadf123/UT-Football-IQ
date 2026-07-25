"""Tests for the player development passport (Issue #7).

Covers the governance shaping (staff/player/recruiting projections), the
identity-confidence experimental gate, and the profile/snapshot HTTP surface.
All DB access is mocked — no live Postgres required, matching the rest of the
backend suite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.governance import VisibilityMode, shape_player_profile
from app.main import app
from app.models import (
    Player,
    PlayerIdentityState,
    PlayerProfile,
    PlayerProfileSnapshot,
    PlayerVisibilityState,
    ProfileGenerationSource,
    SnapshotApprovalStatus,
    User,
    UserRole,
)
from app.routers.player_profiles import compute_experimental, shape_snapshot
from fastapi.testclient import TestClient

# ── Builders ────────────────────────────────────────────────────────────────


def _user(role: UserRole = UserRole.coach, user_id: uuid.UUID | None = None) -> User:
    u = MagicMock(spec=User)
    u.id = user_id or uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _player(
    *,
    player_id: uuid.UUID | None = None,
    visibility_state: PlayerVisibilityState = PlayerVisibilityState.staff_only,
    user_id: uuid.UUID | None = None,
) -> Player:
    p = MagicMock(spec=Player)
    p.id = player_id or uuid.uuid4()
    p.first_name = "Marcus"
    p.last_name = "Carter"
    p.jersey_number = 11
    p.position = "WR"
    p.position_group = "Skill"
    p.is_active = True
    p.user_id = user_id
    p.metadata_ = None
    p.created_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    p.visibility_state = visibility_state
    return p


def _profile(
    *,
    player_id: uuid.UUID,
    profile_id: uuid.UUID | None = None,
    coach_notes: str | None = "Needs to sink hips on breaks.",
) -> PlayerProfile:
    pr = MagicMock(spec=PlayerProfile)
    pr.id = profile_id or uuid.uuid4()
    pr.player_id = player_id
    pr.season = "2026"
    pr.role_summary = "Slot receiver / gunner"
    pr.development_goals = {"route_running": "tighten breaks"}
    pr.coach_notes = coach_notes
    pr.player_summary = "Strong spring camp — keep building."
    pr.benchmark_group = "position_group"
    pr.favorite_clip_ids = [str(uuid.uuid4())]
    pr.generated_by = ProfileGenerationSource.manual
    pr.approved_by = None
    pr.approved_at = None
    pr.created_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    pr.updated_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    return pr


def _snapshot(
    *,
    player_id: uuid.UUID,
    profile_id: uuid.UUID,
    approval_status: SnapshotApprovalStatus = SnapshotApprovalStatus.pending,
    identity_state: PlayerIdentityState = PlayerIdentityState.known,
    identity_confidence: float | None = 0.9,
    experimental_flag: bool = False,
) -> PlayerProfileSnapshot:
    s = MagicMock(spec=PlayerProfileSnapshot)
    s.id = uuid.uuid4()
    s.profile_id = profile_id
    s.player_id = player_id
    s.week_start = date(2026, 5, 4)
    s.summary_metrics = {"execution_rate": 0.72}
    s.strengths = ["release", "tracking"]
    s.development_focus = ["breakpoint angle"]
    s.evidence_clip_ids = [str(uuid.uuid4())]
    s.identity_state = identity_state
    s.identity_confidence = identity_confidence
    s.identity_signals = {"jersey_ocr": 0.4, "appearance": 0.8, "manual_correction": True}
    s.generated_by = ProfileGenerationSource.model_assisted
    s.approval_status = approval_status
    s.approved_by = None
    s.approved_at = None
    s.experimental_flag = experimental_flag
    return s


def _result(*, scalar: Any = None, all_: list[Any] | None = None) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar
    scalars = MagicMock()
    scalars.all.return_value = all_ or []
    r.scalars.return_value = scalars
    return r


def _wire(user: User, results: list[MagicMock]) -> None:
    """Override auth + DB so ``execute`` returns ``results`` in call order."""

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        seq = list(results)

        async def _execute(_stmt: Any) -> Any:
            return seq.pop(0) if seq else _result()

        session.execute = AsyncMock(side_effect=_execute)
        session.flush = AsyncMock()
        session.add = MagicMock()
        yield session

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db


# ── Pure-helper tests ─────────────────────────────────────────────────────────


def test_compute_experimental_gates_low_confidence_and_unknown_identity() -> None:
    th = 0.5
    # Confident, known identity → not experimental.
    assert compute_experimental(PlayerIdentityState.known, 0.9, th) is False
    assert compute_experimental(PlayerIdentityState.probable, 0.6, th) is False
    # Below threshold → experimental even when "known".
    assert compute_experimental(PlayerIdentityState.known, 0.4, th) is True
    # Missing confidence is treated as low confidence.
    assert compute_experimental(PlayerIdentityState.known, None, th) is True
    # Unknown / conflicting / needs_review are always experimental.
    assert compute_experimental(PlayerIdentityState.unknown, 0.99, th) is True
    assert compute_experimental(PlayerIdentityState.conflicting, 0.99, th) is True
    assert compute_experimental(PlayerIdentityState.needs_review, 0.99, th) is True


def test_shape_player_profile_staff_includes_coach_notes() -> None:
    player = _player()
    profile = _profile(player_id=player.id)
    shaped = shape_player_profile(profile, player, VisibilityMode.STAFF)
    assert shaped is not None
    assert shaped["coach_notes"] == "Needs to sink hips on breaks."
    assert shaped["role_summary"] == "Slot receiver / gunner"
    assert shaped["view"] == "staff"


def test_shape_player_profile_player_view_strips_private_content() -> None:
    owner = uuid.uuid4()
    player = _player(visibility_state=PlayerVisibilityState.player_approved, user_id=owner)
    profile = _profile(player_id=player.id)
    actor = _user(role=UserRole.player, user_id=owner)
    shaped = shape_player_profile(profile, player, VisibilityMode.PLAYER, actor=actor)
    assert shaped is not None
    assert "coach_notes" not in shaped
    assert "role_summary" not in shaped
    assert shaped["player_summary"] == "Strong spring camp — keep building."
    assert shaped["view"] == "player"


def test_shape_player_profile_flags_staff_only() -> None:
    # Issue #9: restricted_context_flags are governance metadata — staff
    # projection only, stripped from player and recruiting views.
    owner = uuid.uuid4()
    player = _player(visibility_state=PlayerVisibilityState.recruiting_approved, user_id=owner)
    profile = _profile(player_id=player.id)
    profile.restricted_context_flags = {"acwr": "rehab"}

    staff = shape_player_profile(profile, player, VisibilityMode.STAFF)
    assert staff is not None
    assert staff["restricted_context_flags"] == {"acwr": "rehab"}

    actor = _user(role=UserRole.player, user_id=owner)
    player_view = shape_player_profile(profile, player, VisibilityMode.PLAYER, actor=actor)
    assert player_view is not None
    assert "restricted_context_flags" not in player_view

    recruiting = shape_player_profile(profile, player, VisibilityMode.RECRUITING)
    assert recruiting is not None
    assert "restricted_context_flags" not in recruiting


def test_shape_player_profile_recruiting_blocked_until_approved() -> None:
    player = _player(visibility_state=PlayerVisibilityState.staff_only)
    profile = _profile(player_id=player.id)
    # staff_only player is invisible to recruiting projection.
    assert shape_player_profile(profile, player, VisibilityMode.RECRUITING) is None

    approved = _player(visibility_state=PlayerVisibilityState.recruiting_approved)
    profile2 = _profile(player_id=approved.id)
    shaped = shape_player_profile(profile2, approved, VisibilityMode.RECRUITING)
    assert shaped is not None
    assert "coach_notes" not in shaped
    assert "development_goals" not in shaped
    assert shaped["player_summary"] is not None
    assert shaped["view"] == "recruiting"


def test_shape_snapshot_player_view_hides_identity_and_unapproved() -> None:
    pid = uuid.uuid4()
    prof = uuid.uuid4()
    pending = _snapshot(player_id=pid, profile_id=prof)
    # Pending snapshots are invisible to a player.
    assert shape_snapshot(pending, VisibilityMode.PLAYER) is None

    approved = _snapshot(
        player_id=pid, profile_id=prof, approval_status=SnapshotApprovalStatus.approved
    )
    player_view = shape_snapshot(approved, VisibilityMode.PLAYER)
    assert player_view is not None
    assert "identity_confidence" not in player_view
    assert "identity_state" not in player_view
    assert player_view["strengths"] == ["release", "tracking"]

    staff_view = shape_snapshot(approved, VisibilityMode.STAFF)
    assert staff_view is not None
    assert staff_view["identity_confidence"] == 0.9
    assert staff_view["identity_state"] == "known"


# ── Endpoint tests ──────────────────────────────────────────────────────────


def test_get_profile_staff_returns_full_payload() -> None:
    player = _player()
    profile = _profile(player_id=player.id)
    _wire(_user(UserRole.coach), [_result(scalar=player), _result(scalar=profile)])
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/players/{player.id}/profile", params={"season": "2026"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["coach_notes"] == "Needs to sink hips on breaks."
    assert body["view"] == "staff"


def test_get_profile_player_view_omits_coach_notes() -> None:
    owner = uuid.uuid4()
    player = _player(visibility_state=PlayerVisibilityState.player_approved, user_id=owner)
    profile = _profile(player_id=player.id)
    _wire(
        _user(UserRole.player, user_id=owner),
        [_result(scalar=player), _result(scalar=profile)],
    )
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/players/{player.id}/profile", params={"season": "2026"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "coach_notes" not in body
    assert body["player_summary"] is not None
    assert body["view"] == "player"


def test_get_profile_404_when_missing() -> None:
    player = _player()
    _wire(_user(UserRole.coach), [_result(scalar=player), _result(scalar=None)])
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/players/{player.id}/profile", params={"season": "2026"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_upsert_profile_creates_for_coach() -> None:
    player = _player()
    _wire(_user(UserRole.coach), [_result(scalar=player), _result(scalar=None)])
    try:
        with TestClient(app) as c:
            resp = c.put(
                f"/api/v1/players/{player.id}/profile",
                json={
                    "season": "2026",
                    "role_summary": "Slot / gunner",
                    "coach_notes": "Private note",
                    "player_summary": "Great camp",
                    "development_goals": {"goal": "tighten breaks"},
                    "generated_by": "manual",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["coach_notes"] == "Private note"
    assert body["season"] == "2026"
    assert body["view"] == "staff"


def test_upsert_profile_forbidden_for_player_role() -> None:
    player = _player()
    _wire(_user(UserRole.player), [])
    try:
        with TestClient(app) as c:
            resp = c.put(
                f"/api/v1/players/{player.id}/profile",
                json={"season": "2026"},
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_create_snapshot_low_confidence_is_experimental() -> None:
    player = _player()
    profile = _profile(player_id=player.id)
    _wire(_user(UserRole.coach), [_result(scalar=player), _result(scalar=profile)])
    try:
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/players/{player.id}/profile/snapshots",
                json={
                    "season": "2026",
                    "week_start": "2026-05-04",
                    "identity_state": "unknown",
                    "identity_confidence": 0.2,
                    "strengths": ["release"],
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["experimental"] is True
    assert body["approval_status"] == "pending"


def test_create_snapshot_known_high_confidence_not_experimental() -> None:
    player = _player()
    profile = _profile(player_id=player.id)
    _wire(_user(UserRole.coach), [_result(scalar=player), _result(scalar=profile)])
    try:
        with TestClient(app) as c:
            resp = c.post(
                f"/api/v1/players/{player.id}/profile/snapshots",
                json={
                    "season": "2026",
                    "week_start": "2026-05-04",
                    "identity_state": "known",
                    "identity_confidence": 0.92,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201, resp.text
    assert resp.json()["experimental"] is False


def test_approve_experimental_snapshot_is_blocked() -> None:
    player = _player()
    snap = _snapshot(
        player_id=player.id,
        profile_id=uuid.uuid4(),
        identity_state=PlayerIdentityState.unknown,
        identity_confidence=0.2,
        experimental_flag=True,
    )
    _wire(_user(UserRole.coach), [_result(scalar=snap)])
    try:
        with TestClient(app) as c:
            resp = c.patch(
                f"/api/v1/players/{player.id}/profile/snapshots/{snap.id}/approval",
                json={"status": "approved"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error_code"] == "identity_confidence_too_low"


def test_approve_confident_snapshot_succeeds() -> None:
    player = _player()
    snap = _snapshot(
        player_id=player.id,
        profile_id=uuid.uuid4(),
        identity_state=PlayerIdentityState.known,
        identity_confidence=0.9,
        experimental_flag=False,
    )
    _wire(_user(UserRole.coach), [_result(scalar=snap)])
    try:
        with TestClient(app) as c:
            resp = c.patch(
                f"/api/v1/players/{player.id}/profile/snapshots/{snap.id}/approval",
                json={"status": "approved"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json()["approval_status"] == "approved"


def test_list_snapshots_player_mode_returns_only_approved() -> None:
    owner = uuid.uuid4()
    player = _player(visibility_state=PlayerVisibilityState.player_approved, user_id=owner)
    prof = uuid.uuid4()
    approved = _snapshot(
        player_id=player.id, profile_id=prof, approval_status=SnapshotApprovalStatus.approved
    )
    pending = _snapshot(
        player_id=player.id, profile_id=prof, approval_status=SnapshotApprovalStatus.pending
    )
    _wire(
        _user(UserRole.player, user_id=owner),
        [_result(scalar=player), _result(all_=[approved, pending])],
    )
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/players/{player.id}/profile/snapshots")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(approved.id)
    assert "identity_confidence" not in body[0]
