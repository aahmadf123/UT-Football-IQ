"""Health data-fusion ingestion endpoints (Issue #9).

Manual/CSV-style ingestion for the five UT sources that feed the athlete
health/workload dashboard: wellness self-reports, GPS/wearable daily loads
(Catapult-style), strength & conditioning sessions, the team-level academic
calendar, and coarse injury history.

Governance:

* Writes are gated by ``require_policy(HEALTH_WORKLOAD, WRITE)`` —
  sports-performance staff + admins only (analysts read, never ingest).
* Every ingest emits an audit event carrying the actor, source category, and
  row count — never row contents.
* Injury history stores NO diagnosis free-text by design (body region, side,
  coarse status, days missed only).
* Rows upsert on their natural keys so re-importing a corrected CSV is safe.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.governance import Action, Resource, audit_event, require_policy
from app.models import (
    AcademicCalendarEvent,
    GpsWorkloadDaily,
    InjuryHistory,
    ScSession,
    User,
    WellnessEntry,
)

router = APIRouter(prefix="/api/v1/health-workload/ingest", tags=["health-ingest"])
log = structlog.get_logger(__name__)

_require_health_write = require_policy(Resource.HEALTH_WORKLOAD, Action.WRITE)

_MAX_ROWS = 1000


# ── Row schemas ───────────────────────────────────────────────────────────────


class WellnessRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: uuid.UUID
    date: datetime.date
    soreness: int | None = Field(default=None, ge=1, le=10)
    sleep_hours: float | None = Field(default=None, ge=0.0, le=24.0)
    sleep_quality: int | None = Field(default=None, ge=1, le=10)
    energy: int | None = Field(default=None, ge=1, le=10)
    stress: int | None = Field(default=None, ge=1, le=10)
    source: str = Field(default="manual_csv", max_length=50)


class GpsRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: uuid.UUID
    date: datetime.date
    session_type: Literal["practice", "game", "conditioning"] = "practice"
    total_distance_m: float | None = Field(default=None, ge=0.0)
    high_speed_distance_m: float | None = Field(default=None, ge=0.0)
    sprint_count: int | None = Field(default=None, ge=0)
    max_speed_ms: float | None = Field(default=None, ge=0.0, le=15.0)
    accel_count: int | None = Field(default=None, ge=0)
    decel_count: int | None = Field(default=None, ge=0)
    player_load: float | None = Field(default=None, ge=0.0)
    source: str = Field(default="manual_csv", max_length=50)


class StrengthRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player_id: uuid.UUID
    date: datetime.date
    session_volume: float | None = Field(default=None, ge=0.0)
    tonnage_kg: float | None = Field(default=None, ge=0.0)
    session_rpe: float | None = Field(default=None, ge=0.0, le=10.0)
    duration_min: int | None = Field(default=None, ge=0)
    source: str = Field(default="manual_csv", max_length=50)


class AcademicRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: datetime.date
    end_date: datetime.date | None = None
    event_type: Literal["exams", "midterms", "break", "travel", "other"]
    label: str | None = Field(default=None, max_length=200)
    source: str = Field(default="manual_csv", max_length=50)


class InjuryHistoryRow(BaseModel):
    # NO diagnosis free-text field by design — coarse availability facts only.
    model_config = ConfigDict(extra="forbid")

    player_id: uuid.UUID
    reported_date: datetime.date
    body_region: str = Field(..., max_length=50)
    side: Literal["L", "R"] | None = None
    status: Literal["active", "resolved", "returned"] = "active"
    days_missed: int | None = Field(default=None, ge=0)
    source: str = Field(default="manual_csv", max_length=50)


# One concrete batch model per source (rather than a generic) so the module
# stays importable on Python 3.11 while satisfying ruff's PEP 695 preference
# on 3.12 — there is no shared generic to modernize.
class WellnessBatch(BaseModel):
    rows: list[WellnessRow] = Field(..., max_length=_MAX_ROWS)


class GpsBatch(BaseModel):
    rows: list[GpsRow] = Field(..., max_length=_MAX_ROWS)


class StrengthBatch(BaseModel):
    rows: list[StrengthRow] = Field(..., max_length=_MAX_ROWS)


class AcademicBatch(BaseModel):
    rows: list[AcademicRow] = Field(..., max_length=_MAX_ROWS)


class InjuryHistoryBatch(BaseModel):
    rows: list[InjuryHistoryRow] = Field(..., max_length=_MAX_ROWS)


# ── Upsert helpers ────────────────────────────────────────────────────────────


async def _upsert(
    db: AsyncSession,
    model: type,
    rows: Sequence[BaseModel],
    key_fields: tuple[str, ...],
    actor: User,
) -> int:
    upserted = 0
    for row in rows:
        values = row.model_dump()
        conditions = [getattr(model, field) == values[field] for field in key_fields]
        existing_result: Any = await db.execute(select(model).where(*conditions))
        existing = existing_result.scalar_one_or_none()
        if existing is None:
            db.add(model(created_by=actor.id, **values))
        else:
            for key, value in values.items():
                if key not in key_fields:
                    setattr(existing, key, value)
        upserted += 1
    await db.flush()
    return upserted


def _audit_ingest(user: User, source: str, count: int) -> None:
    audit_event(
        "audit.health_workload.ingest.write",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.WRITE.value,
        ingest_source=source,
        count=count,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/wellness", status_code=status.HTTP_201_CREATED)
async def ingest_wellness(
    body: WellnessBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_write)],
) -> dict[str, Any]:
    """Ingest daily wellness self-reports (workload tier)."""
    count = await _upsert(db, WellnessEntry, body.rows, ("player_id", "date"), user)
    _audit_ingest(user, "wellness", count)
    return {"upserted": count, "source": "wellness"}


@router.post("/gps", status_code=status.HTTP_201_CREATED)
async def ingest_gps(
    body: GpsBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_write)],
) -> dict[str, Any]:
    """Ingest GPS/wearable daily movement loads (workload tier)."""
    count = await _upsert(
        db, GpsWorkloadDaily, body.rows, ("player_id", "date", "session_type"), user
    )
    _audit_ingest(user, "gps_wearables", count)
    return {"upserted": count, "source": "gps_wearables"}


@router.post("/strength", status_code=status.HTTP_201_CREATED)
async def ingest_strength(
    body: StrengthBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_write)],
) -> dict[str, Any]:
    """Ingest strength & conditioning session loads (workload tier)."""
    count = await _upsert(db, ScSession, body.rows, ("player_id", "date"), user)
    _audit_ingest(user, "strength_conditioning", count)
    return {"upserted": count, "source": "strength_conditioning"}


@router.post("/academic", status_code=status.HTTP_201_CREATED)
async def ingest_academic(
    body: AcademicBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_write)],
) -> dict[str, Any]:
    """Ingest team-level academic-calendar events (no per-player data)."""
    count = await _upsert(db, AcademicCalendarEvent, body.rows, ("date", "event_type"), user)
    _audit_ingest(user, "academic_calendar", count)
    return {"upserted": count, "source": "academic_calendar"}


@router.post("/injury-history", status_code=status.HTTP_201_CREATED)
async def ingest_injury_history(
    body: InjuryHistoryBatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(_require_health_write)],
) -> dict[str, Any]:
    """Ingest coarse injury-history rows (rehab tier — restricted)."""
    count = await _upsert(
        db, InjuryHistory, body.rows, ("player_id", "reported_date", "body_region"), user
    )
    _audit_ingest(user, "injury_history", count)
    return {"upserted": count, "source": "injury_history"}
