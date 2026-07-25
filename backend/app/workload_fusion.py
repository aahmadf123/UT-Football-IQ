"""Daily athlete-state fusion for the health/workload dashboard (Issue #9).

Joins the nightly CV workload rollup (``player_workload_daily``, Issue #149)
with the UT fusion sources — wellness self-reports, GPS/wearable loads, S&C
sessions, the team-level academic calendar, and rehab-tier injury history —
into one policy-shaped "daily athlete state" per player.

Structure: :func:`fetch_daily_state_inputs` does the DB reads;
:func:`fuse_daily_athlete_state` is a pure function over those inputs so the
governance shaping (tier gates, player-level vs aggregate, caveats on every
value) is directly unit-testable.

Every fused block carries its ``source`` and a caveat; missing sources are
explicit ``source_unavailable`` entries — values are never presented as
ground truth (Issue #9 policy statement).
"""

from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.governance import can_view_field
from app.health_workload import POLICY_STATEMENT, WORKLOAD_RISK_CAVEAT
from app.models import (
    AcademicCalendarEvent,
    GpsWorkloadDaily,
    InjuryHistory,
    Player,
    PlayerProfile,
    PlayerWorkloadDaily,
    ScSession,
    UserRole,
    WellnessEntry,
)

# Roles that may see the player-level fused view. Mirrors the injury-risk
# endpoint's split: analysts get position-group aggregates only.
PLAYER_LEVEL_STATE_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.admin, UserRole.sportsperformance}
)

WELLNESS_CAVEAT = "Self-reported context — never a clinical assessment."
STAFF_LOGGED_CAVEAT = "Staff-logged training context, shown as entered."


@dataclass
class DailyStateInputs:
    """Raw per-day rows fetched for the fusion (pre-shaping)."""

    date: datetime.date
    workload_rows: list[Any] = field(default_factory=list)  # (PlayerWorkloadDaily, Player)
    wellness_rows: list[Any] = field(default_factory=list)
    gps_rows: list[Any] = field(default_factory=list)
    sc_rows: list[Any] = field(default_factory=list)
    academic_events: list[Any] = field(default_factory=list)
    injury_rows: list[Any] = field(default_factory=list)
    profile_flags: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)


async def fetch_daily_state_inputs(
    db: AsyncSession,
    date: datetime.date,
    *,
    position_group: str | None = None,
) -> DailyStateInputs:
    """Read every fusion source for ``date`` (player-filtered later)."""
    workload_q = (
        select(PlayerWorkloadDaily, Player)
        .join(Player, PlayerWorkloadDaily.player_id == Player.id)
        .where(PlayerWorkloadDaily.date == date)
    )
    if position_group:
        workload_q = workload_q.where(func.upper(Player.position_group) == position_group.upper())
    workload_rows = list((await db.execute(workload_q)).all())

    wellness_rows = list(
        (await db.execute(select(WellnessEntry).where(WellnessEntry.date == date))).scalars().all()
    )
    gps_rows = list(
        (await db.execute(select(GpsWorkloadDaily).where(GpsWorkloadDaily.date == date)))
        .scalars()
        .all()
    )
    sc_rows = list(
        (await db.execute(select(ScSession).where(ScSession.date == date))).scalars().all()
    )
    academic_events = list(
        (
            await db.execute(
                select(AcademicCalendarEvent).where(
                    AcademicCalendarEvent.date <= date,
                    func.coalesce(AcademicCalendarEvent.end_date, AcademicCalendarEvent.date)
                    >= date,
                )
            )
        )
        .scalars()
        .all()
    )
    injury_rows = list(
        (await db.execute(select(InjuryHistory).where(InjuryHistory.status == "active")))
        .scalars()
        .all()
    )

    player_ids = {row.player_id for row, _ in workload_rows}
    profile_flags: dict[str, dict[str, Any] | None] = {}
    if player_ids:
        profiles = list(
            (
                await db.execute(
                    select(PlayerProfile)
                    .where(PlayerProfile.player_id.in_(player_ids))
                    .order_by(PlayerProfile.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        for profile in profiles:  # later rows (newest season) win
            profile_flags[str(profile.player_id)] = getattr(
                profile, "restricted_context_flags", None
            )

    source_counts = {
        "wellness": await _count(db, WellnessEntry),
        "gps_wearables": await _count(db, GpsWorkloadDaily),
        "strength_conditioning": await _count(db, ScSession),
        "academic_calendar": await _count(db, AcademicCalendarEvent),
        "injury_history": await _count(db, InjuryHistory),
    }

    return DailyStateInputs(
        date=date,
        workload_rows=workload_rows,
        wellness_rows=wellness_rows,
        gps_rows=gps_rows,
        sc_rows=sc_rows,
        academic_events=academic_events,
        injury_rows=injury_rows,
        profile_flags=profile_flags,
        source_counts=source_counts,
    )


async def _count(db: AsyncSession, model: type) -> int:
    result = await db.execute(select(func.count()).select_from(model))
    value = result.scalar_one_or_none()
    return int(value or 0)


def fuse_daily_athlete_state(inputs: DailyStateInputs, *, role: UserRole) -> dict[str, Any]:
    """Shape the fused daily athlete state for ``role`` (pure function).

    * ``admin`` / ``sportsperformance`` — player-level rows, tier-gated.
    * everyone else with surface access (``analyst``) — position-group
      aggregates only; no player ids, names, or injury history.
    """
    wellness_by_player = {str(r.player_id): r for r in inputs.wellness_rows}
    gps_by_player = {str(r.player_id): r for r in inputs.gps_rows}
    sc_by_player = {str(r.player_id): r for r in inputs.sc_rows}
    injuries_by_player: dict[str, list[Any]] = {}
    for r in inputs.injury_rows:
        injuries_by_player.setdefault(str(r.player_id), []).append(r)

    academic = [
        {
            "date": _iso(e.date),
            "end_date": _iso(e.end_date),
            "event_type": e.event_type,
            "label": e.label,
            "source": "academic_calendar",
        }
        for e in inputs.academic_events
    ]

    payload: dict[str, Any] = {
        "date": inputs.date.isoformat(),
        "role": role.value,
        "player_level": role in PLAYER_LEVEL_STATE_ROLES,
        "policy_statement": POLICY_STATEMENT,
        "caveat": WORKLOAD_RISK_CAVEAT,
        "academic_context": academic,
        "source_counts": dict(inputs.source_counts),
    }

    if role in PLAYER_LEVEL_STATE_ROLES:
        payload["players"] = [
            _player_state(
                row,
                player,
                role=role,
                flags=inputs.profile_flags.get(str(row.player_id)),
                wellness=wellness_by_player.get(str(row.player_id)),
                gps=gps_by_player.get(str(row.player_id)),
                sc=sc_by_player.get(str(row.player_id)),
                injuries=injuries_by_player.get(str(row.player_id), []),
            )
            for row, player in inputs.workload_rows
        ]
    else:
        payload["aggregates"] = _aggregates(inputs.workload_rows)

    return payload


def _player_state(
    row: Any,
    player: Any,
    *,
    role: UserRole,
    flags: dict[str, Any] | None,
    wellness: Any,
    gps: Any,
    sc: Any,
    injuries: list[Any],
) -> dict[str, Any]:
    caveats: list[str] = []

    state: dict[str, Any] = {
        "player_id": str(row.player_id),
        "name": f"{player.first_name} {player.last_name}",
        "jersey_number": player.jersey_number,
        "position_group": player.position_group,
    }

    if can_view_field(role, "acwr", flags):
        state["cv_workload"] = {
            "daily_load": row.daily_load,
            "sprint_count": row.sprint_count,
            "max_speed_yps": row.max_speed_yps,
            "asymmetry_index": row.asymmetry_index,
            "acwr": row.acwr,
            "injury_risk_score": row.injury_risk_score,
            "risk_reason_codes": row.risk_reason_codes or [],
            "confidence": row.confidence,
            "model_variant": row.model_variant,
            "source": "cv_pipeline",
            "caveat": WORKLOAD_RISK_CAVEAT,
        }
    else:
        state["cv_workload"] = None
        caveats.append("cv_workload:tier_restricted")

    state["wellness"] = _source_block(
        wellness,
        ("soreness", "sleep_hours", "sleep_quality", "energy", "stress"),
        source="wellness",
        caveat=WELLNESS_CAVEAT,
        visible=can_view_field(role, "wellness", flags),
        caveats=caveats,
        unavailable_key="wellness",
    )
    state["gps"] = _source_block(
        gps,
        (
            "session_type",
            "total_distance_m",
            "high_speed_distance_m",
            "sprint_count",
            "max_speed_ms",
            "accel_count",
            "decel_count",
            "player_load",
        ),
        source="gps_wearables",
        caveat=STAFF_LOGGED_CAVEAT,
        visible=can_view_field(role, "gps", flags),
        caveats=caveats,
        unavailable_key="gps",
    )
    state["strength_conditioning"] = _source_block(
        sc,
        ("session_volume", "tonnage_kg", "session_rpe", "duration_min"),
        source="strength_conditioning",
        caveat=STAFF_LOGGED_CAVEAT,
        visible=can_view_field(role, "strength_conditioning", flags),
        caveats=caveats,
        unavailable_key="strength_conditioning",
    )

    # Rehab tier: coarse active-injury context, never diagnosis text.
    if can_view_field(role, "injury_history", flags):
        state["injury_history"] = [
            {
                "reported_date": _iso(i.reported_date),
                "body_region": i.body_region,
                "side": i.side,
                "status": i.status,
                "days_missed": i.days_missed,
                "source": "injury_history",
            }
            for i in injuries
        ]
    else:
        state["injury_history"] = None
        caveats.append("injury_history:tier_restricted")

    state["caveats"] = caveats
    return state


def _source_block(
    row: Any,
    fields: tuple[str, ...],
    *,
    source: str,
    caveat: str,
    visible: bool,
    caveats: list[str],
    unavailable_key: str,
) -> dict[str, Any] | None:
    if not visible:
        caveats.append(f"{unavailable_key}:tier_restricted")
        return None
    if row is None:
        caveats.append(f"{unavailable_key}:source_unavailable")
        return None
    block = {name: getattr(row, name, None) for name in fields}
    block["source"] = source
    block["caveat"] = caveat
    return block


def _aggregates(workload_rows: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, set[Any]] = {}
    for row, player in workload_rows:
        pg = (player.position_group or "UNKNOWN").upper()
        g = groups.setdefault(pg, {"acwr": [], "asymmetry_index": [], "daily_load": []})
        counts.setdefault(pg, set()).add(row.player_id)
        if row.acwr is not None:
            g["acwr"].append(row.acwr)
        if row.asymmetry_index is not None:
            g["asymmetry_index"].append(row.asymmetry_index)
        if row.daily_load is not None:
            g["daily_load"].append(row.daily_load)

    return [
        {
            "position_group": pg,
            "player_count": len(counts[pg]),
            "mean_acwr": _mean(g["acwr"]),
            "mean_asymmetry_index": _mean(g["asymmetry_index"]),
            "mean_daily_load": _mean(g["daily_load"]),
            "caveat": WORKLOAD_RISK_CAVEAT,
        }
        for pg, g in sorted(groups.items())
    ]


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
