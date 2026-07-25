"""Athlete health/workload surface router (Issues #113 + #149).

Exposes the **role-gated, audit-logged** athlete health/workload surface:

* ``GET /surface`` — policy-safe integration/contract status (Issue #113).
* ``GET /injury-risk`` — nightly per-player workload-risk rollups
  (Issue #149): ACWR, gait asymmetry, and the experimental heuristic risk
  score. Player-level rows are limited to sports-performance staff + admins;
  analysts receive position-group aggregates only.
* ``GET /daily-loads`` — per-player daily CV load aggregation consumed by the
  nightly ``workload_rollup`` job (identity-confident tracklets only).
* ``POST /daily`` — bulk upsert of nightly rollup rows from the GPU worker.

Access is gated by the ``health_workload`` policy via
:func:`app.governance.require_policy`, every read/write emits a structured
audit event (identifiers and enums only — never metric values or PII), and
every risk value ships with the non-diagnostic caveat. Player-attributed rows
below the identity-confidence floor are rejected on ingest — low-confidence
identity stays anonymous, never a named-player risk label.

This is intentionally separate from ``GET /api/v1/health/workload`` in
:mod:`app.routers.health`, which reports *system* GPU-queue capacity for the
gating layer.
"""

from __future__ import annotations

import datetime
import statistics
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_analyst_or_above
from app.governance import Action, Resource, audit_event, require_policy
from app.health_workload import (
    MIN_IDENTITY_CONFIDENCE,
    SURFACE_DISCLAIMER,
    WORKLOAD_RISK_CAVEAT,
    build_surface_status,
)
from app.models import Alert, AlertType, Metric, Player, PlayerWorkloadDaily, User, UserRole
from app.workload_fusion import fetch_daily_state_inputs, fuse_daily_athlete_state

router = APIRouter(prefix="/api/v1/health-workload", tags=["health-workload"])
log = structlog.get_logger(__name__)

# Roles that may see player-level (named) workload risk. Analysts keep surface
# access but only receive position-group aggregates — issue #9 scopes
# player-level fatigue/risk to sports-performance staff.
PLAYER_LEVEL_RISK_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.admin, UserRole.sportsperformance}
)

_require_health_read = require_policy(Resource.HEALTH_WORKLOAD, Action.READ)


# ── Schemas ───────────────────────────────────────────────────────────────────


class DailyWorkloadUpsert(BaseModel):
    model_config = {"protected_namespaces": ()}

    player_id: uuid.UUID
    date: datetime.date
    daily_load: float | None = Field(default=None, ge=0.0)
    sprint_count: int | None = Field(default=None, ge=0)
    max_speed_yps: float | None = Field(default=None, ge=0.0)
    asymmetry_index: float | None = Field(default=None, ge=0.0)
    acute_load_7d: float | None = Field(default=None, ge=0.0)
    chronic_load_28d: float | None = Field(default=None, ge=0.0)
    acwr: float | None = Field(default=None, ge=0.0)
    injury_risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_reason_codes: list[str] | None = None
    clip_count: int = Field(default=0, ge=0)
    attribution: str = Field(default="player", max_length=20)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    model_variant: str | None = Field(default=None, max_length=50)


class DailyWorkloadBatch(BaseModel):
    rows: list[DailyWorkloadUpsert] = Field(..., max_length=500)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/surface")
async def health_workload_surface(
    user: Annotated[User, Depends(_require_health_read)],
) -> dict[str, object]:
    """Return the policy-safe athlete health/workload surface status.

    Restricted to sports-performance / analyst / admin roles.  The payload
    contains only the viewer's role, the placeholder integration contracts,
    the approved-role list, and the non-medical disclaimer — never per-athlete
    health data, names, or other PII.
    """
    audit_event(
        "audit.health_workload.surface.read",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.READ.value,
        surface="status",
    )
    return build_surface_status(role=user.role)


@router.get("/injury-risk")
async def injury_risk(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_read)],
    date: datetime.date | None = Query(default=None, description="Rollup day (default: latest)"),
    days: int = Query(default=14, ge=1, le=56, description="Trailing window length in days"),
    position_group: str | None = Query(default=None, max_length=20),
) -> dict[str, Any]:
    """Nightly workload-risk rollups (Issue #149).

    Sports-performance staff and admins receive player-level rows (ACWR,
    asymmetry index, risk score, sprint count, trend series). Analysts receive
    position-group aggregates only — never named players. Coaches, players,
    and viewers are denied by the ``health_workload`` policy. Every value is
    an experimental sports-performance indicator, never a diagnosis.
    """
    if date is None:
        # Use the most recently persisted rollup day rather than today, so the
        # default response is non-empty even before the current day's nightly
        # rollup has run.
        max_date_q = select(func.max(PlayerWorkloadDaily.date))
        if position_group:
            max_date_q = (
                select(func.max(PlayerWorkloadDaily.date))
                .join(Player, PlayerWorkloadDaily.player_id == Player.id)
                .where(func.upper(Player.position_group) == position_group.upper())
            )
        max_date_result = await db.execute(max_date_q)
        as_of = max_date_result.scalar_one_or_none() or datetime.date.today()
    else:
        as_of = date
    since = as_of - datetime.timedelta(days=days - 1)

    q = (
        select(PlayerWorkloadDaily, Player)
        .join(Player, PlayerWorkloadDaily.player_id == Player.id)
        .where(PlayerWorkloadDaily.date >= since, PlayerWorkloadDaily.date <= as_of)
        .order_by(PlayerWorkloadDaily.date.asc())
    )
    if position_group:
        q = q.where(func.upper(Player.position_group) == position_group.upper())

    result = await db.execute(q)
    pairs = list(result.all())

    player_level = user.role in PLAYER_LEVEL_RISK_ROLES
    payload: dict[str, Any] = {
        "role": user.role.value,
        "date": as_of.isoformat(),
        "days": days,
        "player_level": player_level,
        "disclaimer": SURFACE_DISCLAIMER,
        "caveat": WORKLOAD_RISK_CAVEAT,
    }

    if player_level:
        by_player: dict[uuid.UUID, dict[str, Any]] = {}
        for row, player in pairs:
            entry = by_player.setdefault(
                row.player_id,
                {
                    "player_id": str(row.player_id),
                    "name": f"{player.first_name} {player.last_name}",
                    "jersey_number": player.jersey_number,
                    "position_group": player.position_group,
                    "series": [],
                    "caveat": WORKLOAD_RISK_CAVEAT,
                },
            )
            entry["series"].append(_series_point(row))
        rows = list(by_player.values())
        for entry in rows:
            entry["latest"] = entry["series"][-1] if entry["series"] else None
        payload["rows"] = rows
    else:
        payload["aggregates"] = _position_group_aggregates(pairs)

    audit_event(
        "audit.health_workload.injury_risk.read",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.READ.value,
        surface="injury_risk",
        date=as_of.isoformat(),
        count=len(pairs),
    )
    return payload


@router.get("/dashboard")
async def health_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_read)],
    date: datetime.date | None = Query(default=None, description="Day (default: latest rollup)"),
    days: int = Query(default=14, ge=1, le=56, description="Trend window length in days"),
    position_group: str | None = Query(default=None, max_length=20),
) -> dict[str, Any]:
    """Unified daily athlete-state dashboard (Issue #9).

    Fuses the nightly CV workload rollup with the UT sources (wellness, GPS,
    S&C, academic calendar, rehab-tier injury history). Player-level state is
    limited to sports-performance staff + admins; analysts receive
    position-group aggregates. Fatigue flags (unacknowledged workload-risk
    alerts) are included only for sports-performance staff + admins — never
    for coaches or analysts. Every value carries its source and caveat; the
    in-app policy statement ships with every response.
    """
    if date is None:
        latest_result = await db.execute(select(func.max(PlayerWorkloadDaily.date)))
        latest = latest_result.scalar_one_or_none()
        as_of = latest if isinstance(latest, datetime.date) else datetime.date.today()
    else:
        as_of = date

    inputs = await fetch_daily_state_inputs(db, as_of, position_group=position_group)
    payload = fuse_daily_athlete_state(inputs, role=user.role)

    # Position-group workload trends over the trailing window.
    since = as_of - datetime.timedelta(days=days - 1)
    trend_q = (
        select(PlayerWorkloadDaily, Player)
        .join(Player, PlayerWorkloadDaily.player_id == Player.id)
        .where(PlayerWorkloadDaily.date >= since, PlayerWorkloadDaily.date <= as_of)
        .order_by(PlayerWorkloadDaily.date.asc())
    )
    if position_group:
        trend_q = trend_q.where(func.upper(Player.position_group) == position_group.upper())
    trend_rows = list((await db.execute(trend_q)).all())
    payload["trends"] = _position_group_trends(trend_rows)
    payload["days"] = days

    # Fatigue flags: unacknowledged workload-risk alerts, S&C staff only.
    if user.role in PLAYER_LEVEL_RISK_ROLES:
        flags_result = await db.execute(
            select(Alert)
            .where(
                Alert.alert_type == AlertType.workload_risk,
                Alert.is_acknowledged.is_(False),
            )
            .order_by(Alert.created_at.desc())
            .limit(50)
        )
        payload["fatigue_flags"] = [
            {
                "alert_id": str(a.id),
                "player_id": str(a.player_id) if a.player_id else None,
                "position_group": a.position_group,
                "severity": a.severity.value,
                "metric_value": a.metric_value,
                "created_at": a.created_at.isoformat(),
                "caveat": WORKLOAD_RISK_CAVEAT,
            }
            for a in flags_result.scalars().all()
        ]

    # Integration/source status derived from real row counts.
    payload["integrations"] = build_surface_status(
        role=user.role, source_counts=inputs.source_counts
    )["integrations"]

    audit_event(
        "audit.health_workload.dashboard.read",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.READ.value,
        surface="dashboard",
        date=as_of.isoformat(),
        count=len(payload.get("players", payload.get("aggregates", []))),
    )
    return payload


def _position_group_trends(
    pairs: list[Any],
) -> dict[str, list[dict[str, Any]]]:
    """Per-position-group daily mean ACWR / load series for trend charts."""
    by_group_day: dict[str, dict[str, dict[str, list[float]]]] = {}
    for row, player in pairs:
        pg = (player.position_group or "UNKNOWN").upper()
        day = row.date.isoformat()
        bucket = by_group_day.setdefault(pg, {}).setdefault(day, {"acwr": [], "daily_load": []})
        if row.acwr is not None:
            bucket["acwr"].append(row.acwr)
        if row.daily_load is not None:
            bucket["daily_load"].append(row.daily_load)

    trends: dict[str, list[dict[str, Any]]] = {}
    for pg, days_map in sorted(by_group_day.items()):
        trends[pg] = [
            {
                "date": day,
                "mean_acwr": _rounded_mean(bucket["acwr"]),
                "mean_daily_load": _rounded_mean(bucket["daily_load"]),
                "player_count": max(len(bucket["acwr"]), len(bucket["daily_load"])),
            }
            for day, bucket in sorted(days_map.items())
        ]
    return trends


@router.get("/daily-loads")
async def daily_loads(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_analyst_or_above)],
    date: datetime.date = Query(..., description="Day to aggregate (UTC)"),
) -> dict[str, Any]:
    """Per-player daily CV load aggregation for the nightly rollup job.

    Sums the day's ``workload_fusion`` metric rows per player. Only
    player-attributed rows count — the worker already anonymizes tracklets
    below the identity-confidence floor, so anonymous-track rows never roll
    into a named player-day here.
    """
    # Suppressed rows (calibration-unsafe clips) are excluded: data the
    # pipeline already marked unsafe must never drive ACWR/risk rollups.
    q = select(Metric).where(
        Metric.metric_name == "workload_fusion",
        func.date(Metric.created_at) == date,
        Metric.is_suppressed.is_(False),
    )
    result = await db.execute(q)
    metrics = list(result.scalars().all())

    by_player: dict[str, dict[str, Any]] = {}
    for m in metrics:
        value = m.metric_value or {}
        if value.get("attribution") != "player" or not value.get("player_id"):
            continue
        player_id = str(value["player_id"])
        entry = by_player.setdefault(
            player_id,
            {
                "player_id": player_id,
                "position_group": None,
                "daily_load": 0.0,
                "sprint_count": 0,
                "max_speed_yps": None,
                "asymmetry_values": [],
                "asymmetry_weights": [],
                "all_confidences": [],
                "clip_count": 0,
            },
        )
        if entry["position_group"] is None and value.get("position_group"):
            entry["position_group"] = str(value["position_group"]).upper()
        entry["daily_load"] += float(value.get("distance_yards") or 0.0)
        entry["sprint_count"] += int(m.sprint_count or value.get("sprint_count") or 0)
        speed = value.get("max_speed_yps")
        if speed is not None:
            entry["max_speed_yps"] = max(entry["max_speed_yps"] or 0.0, float(speed))
        asym = m.asymmetry_index if m.asymmetry_index is not None else value.get("asymmetry_index")
        # Prefer identity_confidence from the metric payload (the CV pipeline
        # emits it explicitly); fall back to the top-level confidence column.
        raw_conf = value.get("identity_confidence") or (
            m.confidence if m.confidence is not None else value.get("confidence")
        )
        if asym is not None:
            entry["asymmetry_values"].append(float(asym))
            # Keep asymmetry weights aligned with asymmetry_values for the
            # weighted mean; default to 0.5 when the clip has no confidence.
            entry["asymmetry_weights"].append(float(raw_conf) if raw_conf is not None else 0.5)
        if raw_conf is not None:
            # Collect confidence from every clip so the daily rollup confidence
            # reflects all workload_fusion clips, not just those with asymmetry.
            entry["all_confidences"].append(float(raw_conf))
        entry["clip_count"] += 1

    players = []
    for entry in by_player.values():
        asyms = entry.pop("asymmetry_values")
        asym_weights = entry.pop("asymmetry_weights")
        all_confs = entry.pop("all_confidences")
        entry["asymmetry_index"] = _weighted_mean(asyms, asym_weights)
        entry["confidence"] = round(statistics.mean(all_confs), 3) if all_confs else None
        entry["daily_load"] = round(entry["daily_load"], 2)
        players.append(entry)

    audit_event(
        "audit.health_workload.daily_loads.read",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.READ.value,
        surface="daily_loads",
        date=date.isoformat(),
        count=len(players),
    )
    return {"date": date.isoformat(), "players": players}


@router.post("/daily", status_code=status.HTTP_201_CREATED)
async def upsert_daily_workload(
    body: DailyWorkloadBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_analyst_or_above)],
) -> dict[str, Any]:
    """Bulk upsert nightly ``player_workload_daily`` rows (GPU worker path).

    Defense in depth: player-attributed rows must carry identity confidence at
    or above the floor — the request is rejected otherwise, so uncertain CV
    output can never be persisted as a named-player risk row.
    """
    for row in body.rows:
        if row.attribution == "player" and (
            row.confidence is None or row.confidence < MIN_IDENTITY_CONFIDENCE
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "low_identity_confidence",
                    "message": (
                        "Player-attributed workload rows require identity confidence "
                        f">= {MIN_IDENTITY_CONFIDENCE}; keep low-confidence workload "
                        "on the anonymous track instead"
                    ),
                },
            )

    upserted = 0
    for row in body.rows:
        existing_result = await db.execute(
            select(PlayerWorkloadDaily).where(
                PlayerWorkloadDaily.player_id == row.player_id,
                PlayerWorkloadDaily.date == row.date,
            )
        )
        existing = existing_result.scalar_one_or_none()
        values = row.model_dump(exclude={"player_id", "date"})
        if existing is None:
            db.add(PlayerWorkloadDaily(player_id=row.player_id, date=row.date, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        upserted += 1
    await db.flush()

    audit_event(
        "audit.health_workload.daily.write",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.WRITE.value,
        surface="daily_upsert",
        count=upserted,
    )
    return {"upserted": upserted}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _series_point(row: PlayerWorkloadDaily) -> dict[str, Any]:
    return {
        "date": row.date.isoformat(),
        "daily_load": row.daily_load,
        "sprint_count": row.sprint_count,
        "max_speed_yps": row.max_speed_yps,
        "asymmetry_index": row.asymmetry_index,
        "acute_load_7d": row.acute_load_7d,
        "chronic_load_28d": row.chronic_load_28d,
        "acwr": row.acwr,
        "injury_risk_score": row.injury_risk_score,
        "risk_reason_codes": row.risk_reason_codes or [],
        "clip_count": row.clip_count,
        "attribution": row.attribution,
        "confidence": row.confidence,
        "model_variant": row.model_variant,
    }


def _position_group_aggregates(
    pairs: list[Any],
) -> list[dict[str, Any]]:
    """Position-group aggregates for roles without player-level access."""
    groups: dict[str, dict[str, Any]] = {}
    for row, player in pairs:
        pg = (player.position_group or "UNKNOWN").upper()
        g = groups.setdefault(
            pg,
            {
                "position_group": pg,
                "player_ids": set(),
                "acwr_values": [],
                "asymmetry_values": [],
                "risk_values": [],
                "caveat": WORKLOAD_RISK_CAVEAT,
            },
        )
        g["player_ids"].add(row.player_id)
        if row.acwr is not None:
            g["acwr_values"].append(row.acwr)
        if row.asymmetry_index is not None:
            g["asymmetry_values"].append(row.asymmetry_index)
        if row.injury_risk_score is not None:
            g["risk_values"].append(row.injury_risk_score)

    aggregates = []
    for g in groups.values():
        aggregates.append(
            {
                "position_group": g["position_group"],
                "player_count": len(g["player_ids"]),
                "mean_acwr": _rounded_mean(g["acwr_values"]),
                "mean_asymmetry_index": _rounded_mean(g["asymmetry_values"]),
                "mean_injury_risk_score": _rounded_mean(g["risk_values"]),
                "max_injury_risk_score": max(g["risk_values"], default=None),
                "caveat": g["caveat"],
            }
        )
    return sorted(aggregates, key=lambda a: a["position_group"])


def _rounded_mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def _weighted_mean(values: list[float], weights: list[float]) -> float | None:
    if not values:
        return None
    total_weight = sum(weights)
    if total_weight <= 0:
        return round(statistics.mean(values), 3)
    return round(sum(v * w for v, w in zip(values, weights, strict=True)) / total_weight, 3)
