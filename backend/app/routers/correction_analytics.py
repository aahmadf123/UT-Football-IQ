"""Correction analytics router — Phase 2.

Dashboard for tracking which labels are most often corrected,
correction rate decline over time, and directing model improvement.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import CoachCorrection, User

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/correction-analytics", tags=["correction-analytics"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class CorrectionTypeSummary(BaseModel):
    correction_type: str
    total_count: int
    training_eligible_count: int
    exported_count: int


class WeeklyTrend(BaseModel):
    week_start: str
    correction_count: int
    correction_types: dict[str, int]


class CorrectionRateDecline(BaseModel):
    weeks: list[WeeklyTrend]
    overall_trend: str
    decline_detected: bool


class CorrectionAnalyticsResponse(BaseModel):
    total_corrections: int
    by_type: list[CorrectionTypeSummary]
    most_corrected_labels: list[dict[str, Any]]
    rate_decline: CorrectionRateDecline


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/summary", response_model=CorrectionAnalyticsResponse)
async def correction_analytics_summary(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
    weeks: int = Query(default=4, ge=1, le=52),
) -> CorrectionAnalyticsResponse:
    """Return correction analytics summary with rate decline tracking."""
    # Total corrections
    total_result = await db.execute(select(func.count(CoachCorrection.id)))
    total_corrections = total_result.scalar() or 0

    # By type
    type_result = await db.execute(
        select(
            CoachCorrection.correction_type,
            func.count(CoachCorrection.id).label("total"),
            func.count(CoachCorrection.id)
            .filter(CoachCorrection.training_eligible.is_(True))
            .label("eligible"),
            func.count(CoachCorrection.id)
            .filter(CoachCorrection.exported_as_label.is_(True))
            .label("exported"),
        ).group_by(CoachCorrection.correction_type)
    )
    by_type = [
        CorrectionTypeSummary(
            correction_type=str(row[0].value if hasattr(row[0], "value") else row[0]),
            total_count=row[1],
            training_eligible_count=row[2],
            exported_count=row[3],
        )
        for row in type_result.all()
    ]

    # Most corrected labels (top label_value patterns)
    recent_result = await db.execute(
        select(CoachCorrection).order_by(CoachCorrection.created_at.desc()).limit(200)
    )
    recent = list(recent_result.scalars().all())
    label_corrections: Counter[str] = Counter()
    for c in recent:
        key = f"{c.correction_type.value}:{_safe_str(c.original_value)}"
        label_corrections[key] += 1

    most_corrected = [{"label_key": k, "count": v} for k, v in label_corrections.most_common(10)]

    # Weekly trend for rate decline detection
    now = datetime.now(UTC)
    weekly_trends: list[WeeklyTrend] = []

    for w in range(weeks):
        week_end = now - timedelta(weeks=w)
        week_start = week_end - timedelta(weeks=1)
        week_result = await db.execute(
            select(CoachCorrection).where(
                CoachCorrection.created_at >= week_start,
                CoachCorrection.created_at < week_end,
            )
        )
        week_corrections = list(week_result.scalars().all())
        type_counts: dict[str, int] = {}
        for c in week_corrections:
            ct = c.correction_type
            t = ct.value if hasattr(ct, "value") else str(ct)
            type_counts[t] = type_counts.get(t, 0) + 1

        weekly_trends.append(
            WeeklyTrend(
                week_start=week_start.isoformat(),
                correction_count=len(week_corrections),
                correction_types=type_counts,
            )
        )

    weekly_trends.reverse()

    # Detect decline
    counts = [w.correction_count for w in weekly_trends]
    decline_detected = False
    overall_trend = "stable"
    if len(counts) >= 2 and counts[0] > 0:
        if counts[-1] < counts[0] * 0.7:
            decline_detected = True
            overall_trend = "declining"
        elif counts[-1] > counts[0] * 1.3:
            overall_trend = "increasing"

    return CorrectionAnalyticsResponse(
        total_corrections=total_corrections,
        by_type=by_type,
        most_corrected_labels=most_corrected,
        rate_decline=CorrectionRateDecline(
            weeks=weekly_trends,
            overall_trend=overall_trend,
            decline_detected=decline_detected,
        ),
    )


def _safe_str(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, dict):
        return str(val.get("formation") or val.get("route") or val.get("coverage") or str(val)[:50])
    return str(val)[:50]
