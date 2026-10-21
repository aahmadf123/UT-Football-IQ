"""Qualified compatibility reads for legacy frontier metric names.

Uses the shared publication policy and definitions. Only supported evidence
can appear here; experimental results remain outside cohort aggregates and
are unavailable to player/viewer roles. Unknowns are never predictions.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import Clip, Label, Metric, Tracklet, User, UserRole
from app.routers.metrics import FRONTIER_METRIC_NAMES
from app.services.metric_definitions import DEFINITIONS
from app.services.metric_policy import metric_visibility

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/analytics/frontier", tags=["frontier-analytics"])

# Coach-readable definitions surfaced with every response so the UI can show
# "what / why / how to interpret" without hardcoding it in the frontend.
METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    name: {
        "label": str(DEFINITIONS[name]["label"]),
        "definition": str(DEFINITIONS[name]["definition"]),
        "interpretation": (
            "Qualified compatibility output. Experimental values stay outside cohort analytics."
        ),
        "depends_on": "Calibrated evidence and task-specific held-out model approval.",
    }
    for name in sorted(FRONTIER_METRIC_NAMES)
}


class FrontierMetricOut(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    clip_id: uuid.UUID
    tracklet_id: uuid.UUID | None
    metric_name: str
    metric_value: dict[str, Any]
    unit: str | None
    experimental: bool
    analytics_safe: bool
    confidence: float | None
    source: str
    sample_size: int | None
    stability_note: str | None
    position_group: str | None
    created_at: str


class FrontierMetricsResponse(BaseModel):
    metrics: list[FrontierMetricOut]
    total: int
    experimental: bool
    definitions: dict[str, dict[str, str]]
    note: str


def _to_out(metric: Metric, position_group: str | None) -> FrontierMetricOut:
    mv = metric.metric_value or {}
    return FrontierMetricOut(
        id=metric.id,
        clip_id=metric.clip_id,
        tracklet_id=metric.tracklet_id,
        metric_name=metric.metric_name,
        metric_value=mv,
        unit=metric.unit,
        experimental=metric.experimental_flag,
        analytics_safe=metric.analytics_safe,
        confidence=metric.confidence,
        source=str(mv.get("source", "derived")),
        sample_size=mv.get("sample_size"),
        stability_note=mv.get("stability_note"),
        position_group=position_group,
        created_at=metric.created_at.isoformat(),
    )


@router.get("", response_model=FrontierMetricsResponse)
async def list_frontier_metrics(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
    metric_name: str | None = Query(default=None, description="xsep | xyards | xpressure"),
    clip_id: uuid.UUID | None = Query(default=None),
    position_group: str | None = Query(default=None),
    formation: str | None = Query(default=None),
    concept: str | None = Query(default=None),
    opponent: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> FrontierMetricsResponse:
    """List experimental frontier metrics, sliced for coach/analyst review.

    Player and viewer accounts are blocked — these metrics are experimental and
    never player-facing.
    """
    if current_user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Frontier analytics are experimental and coaching-staff only",
        )

    names = FRONTIER_METRIC_NAMES
    if metric_name is not None:
        if metric_name.lower() not in FRONTIER_METRIC_NAMES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown frontier metric '{metric_name}'",
            )
        names = frozenset({metric_name.lower()})

    q = (
        metric_visibility(select(Metric, Tracklet.position_group), current_user)
        .outerjoin(Tracklet, Metric.tracklet_id == Tracklet.id)
        .where(func.lower(Metric.metric_name).in_(names))
        .order_by(Metric.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    if clip_id is not None:
        q = q.where(Metric.clip_id == clip_id)
    if position_group is not None:
        q = q.where(func.upper(Tracklet.position_group) == position_group.upper())
    if opponent is not None:
        # Metric → Clip → Video.opponent_team (both joins are to-one).
        q = q.where(
            exists(
                select(Clip.id)
                .where(Clip.id == Metric.clip_id)
                .where(Clip.video.has(opponent_team=opponent))
            )
        )
    if formation is not None:
        # ``label_value`` is a generic JSON column, so use the dialect-neutral
        # ``.as_string()`` accessor (not JSONB-only ``.astext``).
        q = q.where(
            exists(
                select(Label.id)
                .where(Label.clip_id == Metric.clip_id)
                .where(Label.label_type == "offensive_formation")
                .where(Label.label_value["formation"].as_string() == formation)
            )
        )
    if concept is not None:
        q = q.where(
            exists(
                select(Label.id)
                .where(Label.clip_id == Metric.clip_id)
                .where(Label.label_type == "play_concept")
                .where(Label.label_value["concept"].as_string().ilike(f"%{concept}%"))
            )
        )

    rows = (await db.execute(q)).all()
    metrics = [_to_out(metric, pg) for metric, pg in rows]

    return FrontierMetricsResponse(
        metrics=metrics,
        total=len(metrics),
        experimental=True,
        definitions={
            name: METRIC_DEFINITIONS[name] for name in names if name in METRIC_DEFINITIONS
        },
        note=(
            "Experimental frontier analytics (Issue #10). Values are derived from "
            "tracking/pose/SAM outputs and are NOT validated Toledo results. Use as "
            "directional signal only, alongside film."
        ),
    )
