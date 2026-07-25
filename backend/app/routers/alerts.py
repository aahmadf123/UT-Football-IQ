"""Alert REST router (Issue #16).

Endpoints:
  POST   /api/v1/alerts               — create alert (GPU worker or analyst)
  GET    /api/v1/alerts               — list alerts, filtered by position group
  GET    /api/v1/alerts/{alert_id}    — get a single alert
  PATCH  /api/v1/alerts/{alert_id}/ack — acknowledge an alert

Alert governance (non-negotiable):
  - Alerts are NEVER exposed to players or viewers.
  - Every alert carries: confidence, clip_uri, player_id, timestamp,
    position_group.
  - Filtering by position group ensures each coach sees only their group.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_any_staff, require_coach_or_above
from app.models import Alert, AlertSeverity, AlertType, User, UserRole
from app.position_filter import position_group_filter
from app.routers.alerts_sse import (
    RESTRICTED_ALERT_TYPES,
    WORKLOAD_ALERT_ROLES,
    alert_type_visible_to,
    publish_alert,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class AlertCreate(BaseModel):
    player_id: uuid.UUID | None = None
    clip_id: uuid.UUID | None = None
    position_group: str = Field(..., max_length=20)
    alert_type: AlertType
    severity: AlertSeverity
    confidence: float = Field(..., ge=0.0, le=1.0)
    metric_name: str = Field(..., max_length=255)
    metric_value: dict[str, Any]
    deviation_sd: float | None = None
    clip_uri: str | None = None
    period_name: str | None = Field(default=None, max_length=100)
    session_id: str | None = Field(default=None, max_length=100)
    job_id: uuid.UUID | None = None


class AlertResponse(BaseModel):
    id: uuid.UUID
    player_id: uuid.UUID | None
    clip_id: uuid.UUID | None
    position_group: str
    alert_type: str
    severity: str
    confidence: float
    metric_name: str
    metric_value: dict[str, Any]
    deviation_sd: float | None
    clip_uri: str | None
    period_name: str | None
    session_id: str | None
    job_id: uuid.UUID | None
    is_acknowledged: bool
    acknowledged_by: uuid.UUID | None
    acknowledged_at: str | None
    is_actioned: bool
    actioned_by: uuid.UUID | None
    actioned_at: str | None
    created_at: str

    @classmethod
    def from_orm(cls, a: Alert) -> AlertResponse:
        return cls(
            id=a.id,
            player_id=a.player_id,
            clip_id=a.clip_id,
            position_group=a.position_group,
            alert_type=a.alert_type.value,
            severity=a.severity.value,
            confidence=a.confidence,
            metric_name=a.metric_name,
            metric_value=a.metric_value,
            deviation_sd=a.deviation_sd,
            clip_uri=a.clip_uri,
            period_name=a.period_name,
            session_id=a.session_id,
            job_id=a.job_id,
            is_acknowledged=a.is_acknowledged,
            acknowledged_by=a.acknowledged_by,
            acknowledged_at=a.acknowledged_at.isoformat() if a.acknowledged_at else None,
            is_actioned=a.is_actioned,
            actioned_by=a.actioned_by,
            actioned_at=a.actioned_at.isoformat() if a.actioned_at else None,
            created_at=a.created_at.isoformat(),
        )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("", response_model=AlertResponse, status_code=status.HTTP_201_CREATED)
async def create_alert(
    body: AlertCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_any_staff)],
) -> AlertResponse:
    """Create a coaching alert.

    Called by the GPU worker pipeline after anomaly detection.  Sports
    performance and coaching staff may also create manual alerts.  Players
    and viewers are blocked.
    """
    alert = Alert(
        id=uuid.uuid4(),
        player_id=body.player_id,
        clip_id=body.clip_id,
        position_group=body.position_group.upper(),
        alert_type=body.alert_type,
        severity=body.severity,
        confidence=body.confidence,
        metric_name=body.metric_name,
        metric_value=body.metric_value,
        deviation_sd=body.deviation_sd,
        clip_uri=body.clip_uri,
        period_name=body.period_name,
        session_id=body.session_id,
        job_id=body.job_id,
        is_acknowledged=False,
        is_actioned=False,
        created_at=datetime.now(UTC),
    )
    db.add(alert)
    await db.flush()
    response = AlertResponse.from_orm(alert)

    try:
        publish_alert(response.model_dump(mode="json"))
    except Exception as exc:
        log.warning("alert_sse_publish_failed", alert_id=str(alert.id), error=str(exc))

    log.info(
        "alert_created",
        alert_id=str(alert.id),
        alert_type=body.alert_type.value,
        position_group=alert.position_group,
        severity=body.severity.value,
    )
    return response


@router.get("", response_model=list[AlertResponse])
async def list_alerts(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    effective_pg: Annotated[str | None, Depends(position_group_filter)],
    alert_type: AlertType | None = Query(default=None),
    severity: AlertSeverity | None = Query(default=None),
    session_id: str | None = Query(default=None),
    acknowledged: bool | None = Query(default=None),
    actioned: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AlertResponse]:
    """List coaching alerts.

    Players and viewers are blocked — alerts are coaching-staff only.
    Each coach sees only their position group's alerts (enforced via the
    position_group_filter dependency).
    """
    if current_user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Alerts are not available in player- or viewer-facing views",
        )
    if alert_type is not None and not alert_type_visible_to(current_user.role, alert_type):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This alert type is restricted to sports-performance staff",
        )

    q = select(Alert).order_by(Alert.created_at.desc()).limit(limit).offset(offset)

    if current_user.role not in WORKLOAD_ALERT_ROLES:
        q = q.where(Alert.alert_type.notin_(RESTRICTED_ALERT_TYPES))
    if effective_pg is not None:
        q = q.where(Alert.position_group == effective_pg.upper())
    if alert_type is not None:
        q = q.where(Alert.alert_type == alert_type)
    if severity is not None:
        q = q.where(Alert.severity == severity)
    if session_id is not None:
        q = q.where(Alert.session_id == session_id)
    if acknowledged is not None:
        q = q.where(Alert.is_acknowledged == acknowledged)
    if actioned is not None:
        q = q.where(Alert.is_actioned == actioned)

    result = await db.execute(q)
    return [AlertResponse.from_orm(a) for a in result.scalars().all()]


@router.get("/{alert_id}", response_model=AlertResponse)
async def get_alert(
    alert_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> AlertResponse:
    """Retrieve a single alert by ID.

    Players and viewers are blocked.
    """
    if current_user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Alerts are not available in player- or viewer-facing views",
        )
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if alert is None or not alert_type_visible_to(current_user.role, alert.alert_type):
        # 404 (not 403) for restricted types so their existence never leaks.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return AlertResponse.from_orm(alert)


@router.patch("/{alert_id}/ack", response_model=AlertResponse)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> AlertResponse:
    """Acknowledge an alert — records who acknowledged it and when.

    Only coaching staff and above may acknowledge alerts.
    """
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if alert is None or not alert_type_visible_to(current_user.role, alert.alert_type):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")

    alert.is_acknowledged = True
    alert.acknowledged_by = current_user.id
    alert.acknowledged_at = datetime.now(UTC)
    await db.flush()

    log.info(
        "alert_acknowledged",
        alert_id=str(alert_id),
        acknowledged_by=str(current_user.id),
    )
    return AlertResponse.from_orm(alert)


@router.patch("/{alert_id}/action", response_model=AlertResponse)
async def action_alert(
    alert_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_coach_or_above)],
) -> AlertResponse:
    """Mark an alert as actioned (turned into a practice/scheme follow-up).

    Distinct from acknowledging (Issue #137): acknowledging means the alert was
    seen; actioning means a coach has addressed it. Tracked in the DB so staff
    can see which tendency breaks have been worked. Only coaching staff and
    above may action alerts.
    """
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if alert is None or not alert_type_visible_to(current_user.role, alert.alert_type):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")

    # Apply the same position-group scoping that ``list_alerts`` enforces:
    # admins/analysts may action any alert; coaches/sportsperformance staff
    # may only action alerts inside their assigned position group, so a coach
    # who learns another group's alert UUID cannot mark it addressed on behalf
    # of the owning staff.
    if current_user.role not in (UserRole.admin, UserRole.analyst):
        user_pg: str | None = getattr(current_user, "position_group", None)
        if not user_pg or alert.position_group.upper() != user_pg.upper():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Alert not found",
            )

    alert.is_actioned = True
    alert.actioned_by = current_user.id
    alert.actioned_at = datetime.now(UTC)
    await db.flush()

    log.info(
        "alert_actioned",
        alert_id=str(alert_id),
        actioned_by=str(current_user.id),
        alert_type=alert.alert_type.value,
    )
    return AlertResponse.from_orm(alert)
