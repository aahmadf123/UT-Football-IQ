"""Governance foundation — central RBAC, visibility shaping, and audit logging.

This module is the single place where authorization, data shaping for
player-facing / staff / recruiting contexts, and structured audit logging are
defined.  Routers should depend on the helpers here rather than re-implementing
role checks or trimming response payloads inline.

Three pillars:

1. **RBAC policy** — :class:`Resource` x :class:`Action` -> allowed roles.
   ``policy_allows`` (and the matching :func:`require_policy` dependency)
   centralizes the matrix so it can be audited at a glance.  The default
   posture is *deny* — a resource/action pair must be explicitly listed in
   :data:`POLICY` to be reachable.

2. **Visibility modes** — :class:`VisibilityMode` (``player``, ``staff``,
   ``recruiting``) describes *which* projection of a player record a caller is
   allowed to see.  :func:`resolve_visibility_mode` derives the mode from the
   user role and (optionally) an explicit query-string override that the
   policy allows, and :func:`shape_player` returns the appropriate dict.

3. **Audit logging** — :func:`audit_event` emits structured ``audit.*`` log
   events for access denials, gating decisions, and visibility shaping.  All
   audit logs are deliberately free of PII beyond the actor's UUID and the
   record's UUID; no health metrics, names, or other sensitive payloads are
   ever written to the log line.

See ``docs/governance.md`` for the human-readable contract.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, status

from app.deps import get_current_user
from app.models import Player, PlayerProfile, PlayerVisibilityState, User, UserRole

log = structlog.get_logger("app.governance")


# ── Resource / action policy ──────────────────────────────────────────────────


class Resource(enum.StrEnum):
    """Logical resources protected by the policy module."""

    PLAYER_PROFILE = "player_profile"
    PLAYER_VISIBILITY = "player_visibility"
    PLAYER_DEVELOPMENT = "player_development"
    PLAYER_METRICS = "player_metrics"
    HEALTH_WORKLOAD = "health_workload"
    HEAVY_WORKLOAD = "heavy_workload"
    CFBD_ANALYTICS = "cfbd_analytics"
    PLAY_PREDICTION = "play_prediction"
    PLAYBOOK = "playbook"
    COUNTERFACTUAL = "counterfactual"


class Action(enum.StrEnum):
    READ = "read"
    WRITE = "write"
    APPROVE = "approve"
    TRIGGER = "trigger"


# Default-deny policy table.  Roles not listed for a (Resource, Action) pair
# cannot perform that action.  Keep this table small and explicit — it is the
# single source of truth that reviewers can audit.
POLICY: dict[tuple[Resource, Action], frozenset[UserRole]] = {
    # Anyone authenticated can read the roster identity surface.
    (Resource.PLAYER_PROFILE, Action.READ): frozenset(
        {
            UserRole.admin,
            UserRole.analyst,
            UserRole.coach,
            UserRole.sportsperformance,
            UserRole.player,
            UserRole.viewer,
        }
    ),
    # Only staff/analysts/admins can mutate visibility state.
    (Resource.PLAYER_VISIBILITY, Action.WRITE): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.coach}
    ),
    (Resource.PLAYER_VISIBILITY, Action.APPROVE): frozenset({UserRole.admin, UserRole.analyst}),
    # Player development passport (Issue #7): authoring profile content
    # (role summary, development goals, coach notes, player summary, curated
    # clips) and weekly snapshots is coaching-staff only. Approving a snapshot
    # for player-facing/recruiting use is the same staff set — recruiting
    # *exposure* is additionally gated by the player_visibility:approve row
    # above (admin/analyst) via the parent player's visibility state.
    (Resource.PLAYER_DEVELOPMENT, Action.WRITE): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.coach}
    ),
    (Resource.PLAYER_DEVELOPMENT, Action.APPROVE): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.coach}
    ),
    # Player metrics: staff + the player themselves (filtered by shape_player).
    (Resource.PLAYER_METRICS, Action.READ): frozenset(
        {
            UserRole.admin,
            UserRole.analyst,
            UserRole.coach,
            UserRole.sportsperformance,
            UserRole.player,
        }
    ),
    # Health/workload surface: sports-performance and analytics leads only.
    (Resource.HEALTH_WORKLOAD, Action.READ): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.sportsperformance}
    ),
    # Writing athlete health/workload data (Issue #9 ingestion endpoints:
    # wellness, GPS, S&C, academic calendar, injury history) is restricted to
    # sports-performance staff + admins — analysts read, they never ingest.
    (Resource.HEALTH_WORKLOAD, Action.WRITE): frozenset(
        {UserRole.admin, UserRole.sportsperformance}
    ),
    # Triggering heavy workloads (heavy jobs, embeddings rebuilds, text search):
    # analyst-or-above only.  Subject to additional health/workload gating.
    (Resource.HEAVY_WORKLOAD, Action.TRIGGER): frozenset({UserRole.admin, UserRole.analyst}),
    # Cached CFBD (CollegeFootballData.com) college-football analytics surface
    # (Issue #163). Public college data, but kept to coaching staff — never
    # exposed to player/viewer/recruiting accounts.
    (Resource.CFBD_ANALYTICS, Action.READ): frozenset(
        {
            UserRole.admin,
            UserRole.analyst,
            UserRole.coach,
            UserRole.sportsperformance,
        }
    ),
    # Pre-snap run/pass predictions (Issues #135/#136). Reading the calibrated
    # tendency surface is coaching-staff only — never exposed to player/viewer
    # accounts. Writing (the worker posting a batch of computed predictions, or
    # a coach correcting one) is restricted to analyst-or-above.
    (Resource.PLAY_PREDICTION, Action.READ): frozenset(
        {
            UserRole.admin,
            UserRole.analyst,
            UserRole.coach,
            UserRole.sportsperformance,
        }
    ),
    (Resource.PLAY_PREDICTION, Action.WRITE): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.coach}
    ),
    # Playbook overlays & assignment execution scoring (Issue #15). Reading the
    # overlay/score surface and authoring concepts, links, scores and overrides
    # are both coaching-staff only — execution scores are never exposed to
    # player/viewer accounts (Non-Goal: no player-facing scores without coach
    # mediation), and sports-performance has no tactical-scheme role here.
    (Resource.PLAYBOOK, Action.READ): frozenset({UserRole.admin, UserRole.analyst, UserRole.coach}),
    (Resource.PLAYBOOK, Action.WRITE): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.coach}
    ),
    # Counterfactual coverage simulator (Issue #141). An offline/backend MVP:
    # reading the experimental "what-if vs another coverage" surface is
    # coaching-staff only (tactical scheme), never exposed to player/viewer
    # accounts, and — like the playbook surface — sports-performance has no
    # tactical-scheme role here. There is no coach-facing frontend yet.
    (Resource.COUNTERFACTUAL, Action.READ): frozenset(
        {UserRole.admin, UserRole.analyst, UserRole.coach}
    ),
}


def policy_allows(role: UserRole, resource: Resource, action: Action) -> bool:
    """Return True iff ``role`` is explicitly permitted to perform ``action``
    on ``resource``.  Deny by default."""
    allowed = POLICY.get((resource, action))
    if allowed is None:
        return False
    return role in allowed


def require_policy(resource: Resource, action: Action) -> Callable[..., Coroutine[Any, Any, User]]:
    """FastAPI dependency factory enforcing a (resource, action) policy.

    On denial emits a structured ``audit.access.denied`` event and raises 403.
    """

    async def _dep(user: Annotated[User, Depends(get_current_user)]) -> User:
        if not policy_allows(user.role, resource, action):
            audit_event(
                "audit.access.denied",
                actor_id=user.id,
                actor_role=user.role.value,
                resource=resource.value,
                action=action.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "policy_denied",
                    "resource": resource.value,
                    "action": action.value,
                    "message": (
                        f"Role '{user.role}' is not permitted to {action.value} {resource.value}"
                    ),
                },
            )
        return user

    return _dep


# ── Visibility modes ──────────────────────────────────────────────────────────


class VisibilityMode(enum.StrEnum):
    """How a player record is shaped for the caller.

    * ``staff`` — full internal projection (coaches, sports performance,
      analysts, admins).  Includes health and experimental metrics references.
    * ``player`` — the player's own self-view.  Identity + approved
      development content; never raw biomechanics or health metrics.
    * ``recruiting`` — externally-visible projection.  Only fields that have
      been explicitly approved for recruiting and never sensitive fields.
    """

    STAFF = "staff"
    PLAYER = "player"
    RECRUITING = "recruiting"


class VisibilityState(enum.StrEnum):
    """Lifecycle of a Player's outward-facing profile content.

    Default is ``staff_only`` — content is internal until a coach or analyst
    explicitly approves it for player-facing or recruiting consumption.
    Anything ``archived`` is hidden from non-staff views entirely.

    Values mirror :class:`app.models.PlayerVisibilityState` so callers can
    interchangeably reference either; helpers in this module accept both.
    """

    staff_only = PlayerVisibilityState.staff_only.value
    player_approved = PlayerVisibilityState.player_approved.value
    recruiting_approved = PlayerVisibilityState.recruiting_approved.value
    archived = PlayerVisibilityState.archived.value


# ── Restricted health-context tiers (Issue #9) ────────────────────────────────
# Which roles may see health-context data of each tier. Deny by default:
# the "medical" tier is declared but mapped to NO role — the platform stores
# no medical data and would need an explicit policy change (plus a real
# medical role) before anything in that tier could ever be surfaced.
RESTRICTED_CONTEXT_TIERS: tuple[str, ...] = ("workload", "rehab", "medical")

_TIER_ROLES: dict[str, frozenset[UserRole]] = {
    "workload": frozenset({UserRole.admin, UserRole.analyst, UserRole.sportsperformance}),
    "rehab": frozenset({UserRole.admin, UserRole.sportsperformance}),
    "medical": frozenset(),
}

# Canonical field/category → tier mapping ("define which fields are workload,
# rehab, or medical restricted"). Per-player escalations live in
# player_profiles.restricted_context_flags and may only tighten this mapping.
RESTRICTED_FIELD_TIERS: dict[str, str] = {
    # CV workload rollup fields (#149)
    "daily_load": "workload",
    "acwr": "workload",
    "asymmetry_index": "workload",
    "injury_risk_score": "workload",
    "sprint_count": "workload",
    # Fusion source categories (#9)
    "wellness": "workload",
    "gps": "workload",
    "strength_conditioning": "workload",
    "injury_history": "rehab",
    "rehab_status": "rehab",
    "medical": "medical",
}

# Escalation order — a per-player override may move a field DOWN this list
# (stricter), never up.
_TIER_STRICTNESS: dict[str, int] = {"workload": 0, "rehab": 1, "medical": 2}


def effective_field_tier(field: str, flags: dict[str, Any] | None = None) -> str:
    """Resolve the tier for a health-context field, honoring escalations only.

    ``flags`` is a player's ``restricted_context_flags`` JSON. An override that
    names an unknown tier or would *widen* access is ignored — flags can only
    escalate.
    """
    base = RESTRICTED_FIELD_TIERS.get(field, "workload")
    override = (flags or {}).get(field)
    if isinstance(override, str) and override in _TIER_STRICTNESS:
        if _TIER_STRICTNESS[override] > _TIER_STRICTNESS[base]:
            return override
    return base


def can_view_field(role: UserRole, field: str, flags: dict[str, Any] | None = None) -> bool:
    """Whether ``role`` may see health-context ``field`` for a player."""
    tier = effective_field_tier(field, flags)
    return role in _TIER_ROLES.get(tier, frozenset())


def allowed_context_tiers(role: UserRole) -> frozenset[str]:
    """The health-context tiers whose data ``role`` may see."""
    return frozenset(tier for tier, roles in _TIER_ROLES.items() if role in roles)


# Fields that are *never* exposed to player-facing or recruiting views,
# regardless of approval state.  Used as a hard guard in :func:`shape_player`.
_SENSITIVE_PLAYER_FIELDS: frozenset[str] = frozenset({"metadata", "user_id"})

# Additional fields stripped from the recruiting projection — keep recruiting
# views to identity + public roster facts only.
_NON_RECRUITING_FIELDS: frozenset[str] = frozenset(
    {"is_active", "user_id", "metadata", "created_at", "visibility_state"}
)


# Roles that may *request* a specific visibility mode via query string.
# Other roles always get the mode implied by their role.
_ROLE_CAN_REQUEST_MODE: dict[UserRole, frozenset[VisibilityMode]] = {
    UserRole.admin: frozenset(
        {VisibilityMode.STAFF, VisibilityMode.PLAYER, VisibilityMode.RECRUITING}
    ),
    UserRole.analyst: frozenset(
        {VisibilityMode.STAFF, VisibilityMode.PLAYER, VisibilityMode.RECRUITING}
    ),
    UserRole.coach: frozenset({VisibilityMode.STAFF, VisibilityMode.RECRUITING}),
    UserRole.sportsperformance: frozenset({VisibilityMode.STAFF}),
}


def default_mode_for_role(role: UserRole) -> VisibilityMode:
    """Map a role to its implicit visibility mode."""
    if role in (UserRole.admin, UserRole.analyst, UserRole.coach, UserRole.sportsperformance):
        return VisibilityMode.STAFF
    if role == UserRole.player:
        return VisibilityMode.PLAYER
    # viewer + any unknown role → recruiting-safe default.
    return VisibilityMode.RECRUITING


def resolve_visibility_mode(user: User, requested: VisibilityMode | None) -> VisibilityMode:
    """Resolve the effective visibility mode for ``user``.

    If ``requested`` is None the default for the user's role is returned.
    If ``requested`` is set, the caller may only downgrade to a mode they are
    explicitly allowed to request — otherwise 403.
    """
    if requested is None:
        return default_mode_for_role(user.role)

    allowed = _ROLE_CAN_REQUEST_MODE.get(user.role, frozenset())
    # Players can always request their own player mode, viewers always recruiting.
    if user.role == UserRole.player and requested == VisibilityMode.PLAYER:
        return VisibilityMode.PLAYER
    if user.role == UserRole.viewer and requested == VisibilityMode.RECRUITING:
        return VisibilityMode.RECRUITING
    if requested in allowed:
        return requested

    audit_event(
        "audit.visibility.mode_denied",
        actor_id=user.id,
        actor_role=user.role.value,
        requested_mode=requested.value,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error_code": "visibility_mode_denied",
            "requested_mode": requested.value,
            "message": (f"Role '{user.role}' may not request visibility mode '{requested.value}'."),
        },
    )


def _normalize_state(value: Any) -> VisibilityState:
    """Coerce a PlayerVisibilityState (or string) into a :class:`VisibilityState`."""
    if value is None:
        return VisibilityState.staff_only
    if isinstance(value, VisibilityState):
        return value
    if isinstance(value, PlayerVisibilityState):
        return VisibilityState(value.value)
    if isinstance(value, str):
        try:
            return VisibilityState(value)
        except ValueError:
            return VisibilityState.staff_only
    return VisibilityState.staff_only


def _player_base_payload(p: Player) -> dict[str, Any]:
    """Convert a Player ORM row to a dict containing every public field.

    Kept in one place so the response shape stays consistent across modes.
    """
    return {
        "id": str(p.id),
        "first_name": p.first_name,
        "last_name": p.last_name,
        "jersey_number": p.jersey_number,
        "position": p.position,
        "position_group": p.position_group,
        "is_active": p.is_active,
        "user_id": str(p.user_id) if p.user_id else None,
        "metadata": p.metadata_,
        "created_at": p.created_at.isoformat(),
        "visibility_state": _normalize_state(
            getattr(p, "visibility_state", VisibilityState.staff_only)
        ).value,
    }


def shape_player(
    player: Player,
    mode: VisibilityMode,
    *,
    actor: User | None = None,
) -> dict[str, Any] | None:
    """Return the payload for ``player`` projected for the given mode.

    Returns ``None`` when the player record's visibility state prohibits this
    mode (e.g. recruiting view for a player still ``staff_only``).  Callers
    that need a hard error can translate ``None`` into a 404.

    Sensitive fields (metadata, raw health metrics, internal IDs) are stripped
    for non-staff modes regardless of column presence — defense in depth.
    """
    state = _normalize_state(getattr(player, "visibility_state", VisibilityState.staff_only))

    if state == VisibilityState.archived and mode != VisibilityMode.STAFF:
        if actor is not None:
            audit_event(
                "audit.visibility.archived_hidden",
                actor_id=actor.id,
                actor_role=actor.role.value,
                player_id=player.id,
                requested_mode=mode.value,
            )
        return None

    base = _player_base_payload(player)

    if mode == VisibilityMode.STAFF:
        # Full payload, plus the recommended view for downstream UIs.
        return {**base, "view": VisibilityMode.STAFF.value}

    if mode == VisibilityMode.PLAYER:
        if actor is not None and actor.role == UserRole.player and player.user_id != actor.id:
            audit_event(
                "audit.visibility.player_view_blocked",
                actor_id=actor.id,
                actor_role=actor.role.value,
                player_id=player.id,
                reason="owner_mismatch",
            )
            return None
        # Players see themselves only after a coach/analyst approves the
        # outward-facing profile (player_approved or recruiting_approved).
        if state == VisibilityState.staff_only:
            if actor is not None:
                audit_event(
                    "audit.visibility.player_view_blocked",
                    actor_id=actor.id,
                    actor_role=actor.role.value,
                    player_id=player.id,
                    state=state.value,
                )
            return None
        payload = {k: v for k, v in base.items() if k not in _SENSITIVE_PLAYER_FIELDS}
        payload["view"] = VisibilityMode.PLAYER.value
        return payload

    if mode == VisibilityMode.RECRUITING:
        if state != VisibilityState.recruiting_approved:
            if actor is not None:
                audit_event(
                    "audit.visibility.recruiting_blocked",
                    actor_id=actor.id,
                    actor_role=actor.role.value,
                    player_id=player.id,
                    state=state.value,
                )
            return None
        # Pre-merged exclusion set so list views don't pay for two membership
        # checks per record.
        excluded = _SENSITIVE_PLAYER_FIELDS | _NON_RECRUITING_FIELDS
        payload = {k: v for k, v in base.items() if k not in excluded}
        payload["view"] = VisibilityMode.RECRUITING.value
        return payload

    # Should be unreachable given the enum is exhaustive above.
    return None


def _iso(value: Any) -> str | None:
    """Return an ISO-8601 string for a datetime-like value, else ``None``."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _enum_value(value: Any) -> Any:
    """Return ``value.value`` for enums (and enum-like), else ``value``."""
    return getattr(value, "value", value)


def shape_player_profile(
    profile: PlayerProfile,
    player: Player,
    mode: VisibilityMode,
    *,
    actor: User | None = None,
) -> dict[str, Any] | None:
    """Project a player development profile (Issue #7) for ``mode``.

    The outward-facing lifecycle gate is delegated to :func:`shape_player` on
    the parent ``player`` — if the player record's visibility state prohibits
    the mode (``archived``, not yet ``player_approved`` for a self-view, not
    ``recruiting_approved`` for an external view, or an owner mismatch), this
    returns ``None`` so callers can translate it into a 404.

    ``coach_notes`` is private staff content and is **never** present in the
    player or recruiting projections, regardless of column state — the staff
    projection is the only one that includes it.
    """
    if shape_player(player, mode, actor=actor) is None:
        return None

    if mode == VisibilityMode.STAFF:
        return {
            "id": str(profile.id),
            "player_id": str(profile.player_id),
            "season": profile.season,
            "role_summary": profile.role_summary,
            "development_goals": profile.development_goals,
            "coach_notes": profile.coach_notes,
            "player_summary": profile.player_summary,
            "benchmark_group": profile.benchmark_group,
            "favorite_clip_ids": profile.favorite_clip_ids or [],
            "generated_by": _enum_value(profile.generated_by),
            "approved_by": str(profile.approved_by) if profile.approved_by else None,
            "approved_at": _iso(profile.approved_at),
            # Health-context tier escalations (Issue #9) — governance metadata
            # for staff; stripped from every non-staff projection below.
            "restricted_context_flags": getattr(profile, "restricted_context_flags", None),
            "created_at": _iso(profile.created_at),
            "updated_at": _iso(profile.updated_at),
            "view": VisibilityMode.STAFF.value,
        }

    if mode == VisibilityMode.PLAYER:
        # Player self-view: coach-approved development content only. No private
        # coach notes, no internal role/authoring bookkeeping.
        return {
            "id": str(profile.id),
            "player_id": str(profile.player_id),
            "season": profile.season,
            "player_summary": profile.player_summary,
            "development_goals": profile.development_goals,
            "favorite_clip_ids": profile.favorite_clip_ids or [],
            "view": VisibilityMode.PLAYER.value,
        }

    if mode == VisibilityMode.RECRUITING:
        # External recruiting view: identity-level proof points and approved
        # highlights only — never private notes or internal development goals.
        return {
            "player_id": str(profile.player_id),
            "season": profile.season,
            "player_summary": profile.player_summary,
            "favorite_clip_ids": profile.favorite_clip_ids or [],
            "view": VisibilityMode.RECRUITING.value,
        }

    # Unreachable — the enum is exhaustive above.
    return None


# ── Audit logging ─────────────────────────────────────────────────────────────


_AUDIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "actor_id",
        "actor_role",
        "resource",
        "action",
        "requested_mode",
        "effective_mode",
        "player_id",
        "state",
        "previous_state",
        "next_state",
        "reason",
        "signal",
        "queue_depth",
        "queue_threshold",
        "endpoint",
        "decision",
        # Athlete health/workload surface (Issue #113) — a coarse surface label
        # only (e.g. "status"); never athlete metrics, names, or other PII.
        "surface",
        # Player development passport (Issue #7) — identifiers/enums only,
        # never profile content, metrics, or notes.
        "season",
        "snapshot_id",
        "identity_state",
        "approval_status",
        "generated_by",
        "experimental",
        # Playbook overlays & assignment scoring (Issue #15) — identifiers and
        # enums only, never coaching points, comments, or trajectory data.
        "clip_id",
        "concept_id",
        "score_id",
        "assignment_key",
        "grade",
        "source",
        "count",
        # Counterfactual coverage simulator (Issue #141) — route/coverage concept
        # keys and data-sufficiency tier only, never trajectories or player names.
        "route_concept",
        "coverage_type",
        "data_sufficiency",
        # Workload-risk surface (Issue #149) — identifiers/enums only: the
        # requested day, the alert-type enum, the model-router variant string,
        # and the attribution mode. Never ACWR/asymmetry/risk values.
        "date",
        "alert_type",
        "model_variant",
        "attribution",
        # Health-context governance (Issue #9) — the restricted tier name and
        # the ingest source category. Never row contents.
        "tier",
        "ingest_source",
    }
)


def _safe_value(value: Any) -> Any:
    """Coerce common UUID / enum types to strings; pass primitives through.

    Anything else is dropped — audit logs must never accidentally carry rich
    objects that could include PII or large blobs.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, str | int | float | bool):
        return value
    # Lists of primitives / UUIDs are allowed (e.g. signal lists).
    if isinstance(value, list | tuple):
        return [_safe_value(v) for v in value]
    return None


def audit_event(event: str, **fields: Any) -> None:
    """Emit a structured audit log line.

    Only keys in :data:`_AUDIT_ALLOWED_KEYS` are persisted, and values are
    coerced to scalar types — this keeps medical/PII fields from ever ending
    up in the log stream by mistake.
    """
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in _AUDIT_ALLOWED_KEYS:
            continue
        safe = _safe_value(value)
        if safe is None and value is not None:
            continue
        payload[key] = safe
    log.info(event, **payload)
