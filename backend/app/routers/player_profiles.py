"""Player development passport router (Issue #7).

The longitudinal, governance-gated player profile: a per-season
:class:`~app.models.PlayerProfile` plus weekly
:class:`~app.models.PlayerProfileSnapshot` rows.

Three audiences share these endpoints; the projection is selected per request
via ``?mode=`` and shaped server-side by :mod:`app.governance`:

* **staff** (coach/analyst/admin/sports-performance) — full payload including
  private ``coach_notes`` and the confidence-scored identity signals.
* **player** — coach-approved development content only; never coach notes,
  identity confidence, or unapproved snapshots.
* **recruiting** — external proof points and approved highlights only.

Authoring profile content and weekly snapshots requires
``player_development:write`` (coaching staff); approving a snapshot for
player-facing/recruiting use requires ``player_development:approve``. A
snapshot whose identity is below the confidence threshold or whose identity
state is not ``known``/``probable`` is flagged experimental and **cannot** be
approved — low-confidence identities are never silently attached to a profile.

Outward-facing visibility is governed by ``players.visibility_state``
(Issue #114). Identity *corrections* ride on the existing tracklet/coach-
correction flywheel (``PATCH /api/v1/tracklets/{id}``), not on this router.

Endpoints:
    GET   /api/v1/players/{id}/profile                          — read (shaped)
    PUT   /api/v1/players/{id}/profile                          — upsert (staff)
    GET   /api/v1/players/{id}/profile/snapshots                — list (shaped)
    POST  /api/v1/players/{id}/profile/snapshots                — create (staff)
    PATCH /api/v1/players/{id}/profile/snapshots/{sid}/approval — approve/reject
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.governance import (
    Action,
    Resource,
    VisibilityMode,
    audit_event,
    require_policy,
    resolve_visibility_mode,
    shape_player,
    shape_player_profile,
)
from app.models import (
    Player,
    PlayerIdentityState,
    PlayerProfile,
    PlayerProfileSnapshot,
    ProfileGenerationSource,
    SnapshotApprovalStatus,
    User,
)

log = structlog.get_logger(__name__)
router = APIRouter(tags=["player-profiles"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class ProfileUpsert(BaseModel):
    """Request body for :func:`upsert_profile`."""

    season: str = Field(min_length=1, max_length=16)
    role_summary: str | None = None
    development_goals: dict[str, Any] | None = None
    coach_notes: str | None = None
    player_summary: str | None = None
    benchmark_group: str | None = Field(default=None, max_length=40)
    favorite_clip_ids: list[uuid.UUID] | None = None
    generated_by: ProfileGenerationSource = ProfileGenerationSource.manual


class SnapshotCreate(BaseModel):
    """Request body for :func:`create_snapshot`."""

    season: str = Field(min_length=1, max_length=16)
    week_start: date
    summary_metrics: dict[str, Any] | None = None
    strengths: list[str] | None = None
    development_focus: list[str] | None = None
    evidence_clip_ids: list[uuid.UUID] | None = None
    identity_state: PlayerIdentityState = PlayerIdentityState.needs_review
    identity_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    identity_signals: dict[str, Any] | None = None
    generated_by: ProfileGenerationSource = ProfileGenerationSource.model_assisted


class ApprovalUpdate(BaseModel):
    """Request body for :func:`update_snapshot_approval`."""

    status: SnapshotApprovalStatus
    reason: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

# Identity states confident enough to be eligible for player-facing approval.
_CONFIDENT_IDENTITY_STATES = frozenset({PlayerIdentityState.known, PlayerIdentityState.probable})


def compute_experimental(
    identity_state: PlayerIdentityState,
    identity_confidence: float | None,
    threshold: float,
) -> bool:
    """Return True when a snapshot must be treated as experimental.

    A snapshot is experimental — and therefore not approvable for player-facing
    use — when the resolved identity is below the confidence threshold or the
    identity state is not ``known``/``probable``. Jersey OCR is never trusted as
    ground truth, so an absent confidence is treated as low confidence.
    """
    if identity_state not in _CONFIDENT_IDENTITY_STATES:
        return True
    if identity_confidence is None:
        return True
    return identity_confidence < threshold


def _clip_ids_to_str(values: list[uuid.UUID] | None) -> list[str] | None:
    if values is None:
        return None
    return [str(v) for v in values]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def shape_snapshot(snapshot: PlayerProfileSnapshot, mode: VisibilityMode) -> dict[str, Any] | None:
    """Project a development snapshot for ``mode``.

    Returns ``None`` for non-staff modes when the snapshot is not approved, so
    list views simply skip it. Staff see the full row (identity confidence,
    signals, approval bookkeeping). Player/recruiting views never receive
    identity internals or unapproved content.
    """
    approval = _enum_value(snapshot.approval_status)
    experimental = bool(snapshot.experimental_flag)

    if mode == VisibilityMode.STAFF:
        return {
            "id": str(snapshot.id),
            "profile_id": str(snapshot.profile_id),
            "player_id": str(snapshot.player_id),
            "week_start": _iso(snapshot.week_start),
            "summary_metrics": snapshot.summary_metrics,
            "strengths": snapshot.strengths or [],
            "development_focus": snapshot.development_focus or [],
            "evidence_clip_ids": snapshot.evidence_clip_ids or [],
            "identity_state": _enum_value(snapshot.identity_state),
            "identity_confidence": snapshot.identity_confidence,
            "identity_signals": snapshot.identity_signals,
            "generated_by": _enum_value(snapshot.generated_by),
            "approval_status": approval,
            "approved_by": str(snapshot.approved_by) if snapshot.approved_by else None,
            "approved_at": _iso(snapshot.approved_at),
            "experimental": experimental,
            "view": VisibilityMode.STAFF.value,
        }

    # Non-staff: only approved snapshots are ever surfaced.
    if approval != SnapshotApprovalStatus.approved.value:
        return None

    if mode == VisibilityMode.PLAYER:
        return {
            "id": str(snapshot.id),
            "week_start": _iso(snapshot.week_start),
            "summary_metrics": snapshot.summary_metrics,
            "strengths": snapshot.strengths or [],
            "development_focus": snapshot.development_focus or [],
            "evidence_clip_ids": snapshot.evidence_clip_ids or [],
            "experimental": experimental,
            "view": VisibilityMode.PLAYER.value,
        }

    # Recruiting: identity-level proof points and approved highlights only.
    return {
        "id": str(snapshot.id),
        "week_start": _iso(snapshot.week_start),
        "summary_metrics": snapshot.summary_metrics,
        "strengths": snapshot.strengths or [],
        "evidence_clip_ids": snapshot.evidence_clip_ids or [],
        "experimental": experimental,
        "view": VisibilityMode.RECRUITING.value,
    }


async def _get_player_or_404(db: AsyncSession, player_id: uuid.UUID) -> Player:
    result = await db.execute(select(Player).where(Player.id == player_id))
    player = result.scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found")
    return player


async def _get_profile(db: AsyncSession, player_id: uuid.UUID, season: str) -> PlayerProfile | None:
    result = await db.execute(
        select(PlayerProfile).where(
            PlayerProfile.player_id == player_id,
            PlayerProfile.season == season,
        )
    )
    return result.scalar_one_or_none()


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/v1/players/{player_id}/profile")
async def get_profile(
    player_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAYER_PROFILE, Action.READ))],
    season: Annotated[str, Query(min_length=1, max_length=16)],
    mode: Annotated[
        VisibilityMode | None,
        Query(description="Visibility mode override (staff|player|recruiting)."),
    ] = None,
) -> dict[str, Any]:
    """Return a player's per-season development profile, shaped for the caller.

    Returns 404 when the player or the season's profile does not exist, or when
    the caller's resolved visibility mode is not permitted for this player
    (so external callers cannot probe which records exist).
    """
    effective_mode = resolve_visibility_mode(current_user, mode)
    player = await _get_player_or_404(db, player_id)
    profile = await _get_profile(db, player_id, season)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Player profile not found"
        )

    shaped = shape_player_profile(profile, player, effective_mode, actor=current_user)
    if shaped is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Player profile not found"
        )

    audit_event(
        "audit.profile.read",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        player_id=player.id,
        effective_mode=effective_mode.value,
        season=season,
        endpoint="player_profiles.get",
    )
    return shaped


@router.put("/api/v1/players/{player_id}/profile")
async def upsert_profile(
    player_id: uuid.UUID,
    body: ProfileUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_policy(Resource.PLAYER_DEVELOPMENT, Action.WRITE))
    ],
) -> dict[str, Any]:
    """Create or update the per-season profile for a player (staff only)."""
    player = await _get_player_or_404(db, player_id)
    profile = await _get_profile(db, player_id, body.season)

    created = profile is None
    if profile is None:
        profile = PlayerProfile(
            id=uuid.uuid4(),
            player_id=player.id,
            season=body.season,
        )
        db.add(profile)

    profile.role_summary = body.role_summary
    profile.development_goals = body.development_goals
    profile.coach_notes = body.coach_notes
    profile.player_summary = body.player_summary
    profile.benchmark_group = body.benchmark_group
    profile.favorite_clip_ids = _clip_ids_to_str(body.favorite_clip_ids)
    profile.generated_by = body.generated_by
    await db.flush()

    audit_event(
        "audit.profile.upserted",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        player_id=player.id,
        season=body.season,
        generated_by=body.generated_by.value,
        decision="created" if created else "updated",
        endpoint="player_profiles.upsert",
    )
    # The author always sees the full staff projection of what they just wrote.
    shaped = shape_player_profile(profile, player, VisibilityMode.STAFF, actor=current_user)
    assert shaped is not None  # staff projection is never gated out
    return shaped


@router.get("/api/v1/players/{player_id}/profile/snapshots")
async def list_snapshots(
    player_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAYER_PROFILE, Action.READ))],
    season: Annotated[str | None, Query(max_length=16)] = None,
    mode: Annotated[VisibilityMode | None, Query()] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return weekly snapshots for a player, shaped for the caller's mode.

    Non-staff callers only ever receive approved snapshots, and only when the
    player's visibility state permits their mode (otherwise an empty list —
    the same information a non-existent player yields, so existence isn't
    leaked).
    """
    effective_mode = resolve_visibility_mode(current_user, mode)
    player = await _get_player_or_404(db, player_id)

    # Lifecycle gate: if the player's visibility state hides this mode, return
    # nothing rather than 404 (a list endpoint shouldn't distinguish empty from
    # forbidden for external callers).
    if shape_player(player, effective_mode, actor=current_user) is None:
        return []

    stmt = select(PlayerProfileSnapshot).where(PlayerProfileSnapshot.player_id == player_id)
    if season is not None:
        profile = await _get_profile(db, player_id, season)
        if profile is None:
            return []
        stmt = stmt.where(PlayerProfileSnapshot.profile_id == profile.id)
    stmt = stmt.order_by(PlayerProfileSnapshot.week_start.desc()).limit(limit)

    result = await db.execute(stmt)
    out: list[dict[str, Any]] = []
    for snap in result.scalars().all():
        shaped = shape_snapshot(snap, effective_mode)
        if shaped is not None:
            out.append(shaped)

    audit_event(
        "audit.profile.read",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        player_id=player.id,
        effective_mode=effective_mode.value,
        season=season,
        endpoint="player_profiles.list_snapshots",
    )
    return out


@router.post("/api/v1/players/{player_id}/profile/snapshots", status_code=status.HTTP_201_CREATED)
async def create_snapshot(
    player_id: uuid.UUID,
    body: SnapshotCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_policy(Resource.PLAYER_DEVELOPMENT, Action.WRITE))
    ],
) -> dict[str, Any]:
    """Create a weekly development snapshot under a player's season profile.

    The snapshot starts ``pending`` and is forced experimental when its
    identity confidence is below the configured threshold or its identity state
    is not ``known``/``probable`` — such a snapshot cannot later be approved for
    player-facing use.
    """
    player = await _get_player_or_404(db, player_id)
    profile = await _get_profile(db, player_id, body.season)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Player profile not found for season; create the profile first.",
        )

    threshold = get_settings().player_profile_identity_confidence_threshold
    experimental = compute_experimental(body.identity_state, body.identity_confidence, threshold)

    snapshot = PlayerProfileSnapshot(
        id=uuid.uuid4(),
        profile_id=profile.id,
        player_id=player.id,
        week_start=body.week_start,
        summary_metrics=body.summary_metrics,
        strengths=body.strengths,
        development_focus=body.development_focus,
        evidence_clip_ids=_clip_ids_to_str(body.evidence_clip_ids),
        identity_state=body.identity_state,
        identity_confidence=body.identity_confidence,
        identity_signals=body.identity_signals,
        generated_by=body.generated_by,
        approval_status=SnapshotApprovalStatus.pending,
        experimental_flag=experimental,
    )
    db.add(snapshot)
    await db.flush()

    audit_event(
        "audit.profile.snapshot_created",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        player_id=player.id,
        snapshot_id=snapshot.id,
        season=body.season,
        identity_state=body.identity_state.value,
        generated_by=body.generated_by.value,
        experimental=experimental,
        endpoint="player_profiles.create_snapshot",
    )
    shaped = shape_snapshot(snapshot, VisibilityMode.STAFF)
    assert shaped is not None  # staff projection is never gated out
    return shaped


@router.patch("/api/v1/players/{player_id}/profile/snapshots/{snapshot_id}/approval")
async def update_snapshot_approval(
    player_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    body: ApprovalUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_policy(Resource.PLAYER_DEVELOPMENT, Action.APPROVE))
    ],
) -> dict[str, Any]:
    """Approve or reject a snapshot for player-facing/recruiting use.

    Approving an experimental snapshot (low-confidence / unknown identity) is
    refused with 409 — low-confidence identities are never silently attached to
    a player-facing profile. ``pending`` cannot be set through this endpoint.
    """
    if body.status == SnapshotApprovalStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_approval_status",
                "message": "Use 'approved' or 'rejected'; a snapshot cannot be reset to pending.",
            },
        )

    result = await db.execute(
        select(PlayerProfileSnapshot).where(
            PlayerProfileSnapshot.id == snapshot_id,
            PlayerProfileSnapshot.player_id == player_id,
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")

    if body.status == SnapshotApprovalStatus.approved and bool(snapshot.experimental_flag):
        audit_event(
            "audit.profile.snapshot_approval_blocked",
            actor_id=current_user.id,
            actor_role=current_user.role.value,
            player_id=player_id,
            snapshot_id=snapshot.id,
            identity_state=_enum_value(snapshot.identity_state),
            decision="blocked",
            endpoint="player_profiles.approve_snapshot",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "identity_confidence_too_low",
                "message": (
                    "Snapshot identity is experimental (low confidence or unknown identity) "
                    "and cannot be approved for player-facing use. Resolve identity via a "
                    "coach correction first."
                ),
            },
        )

    snapshot.approval_status = body.status
    snapshot.approved_by = current_user.id
    snapshot.approved_at = datetime.now(UTC)
    await db.flush()

    audit_event(
        "audit.profile.snapshot_approval_changed",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        player_id=player_id,
        snapshot_id=snapshot.id,
        approval_status=body.status.value,
        reason=body.reason,
        endpoint="player_profiles.approve_snapshot",
    )
    shaped = shape_snapshot(snapshot, VisibilityMode.STAFF)
    assert shaped is not None  # staff projection is never gated out
    return shaped
