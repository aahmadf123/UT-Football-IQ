"""Health-check router — /health, /ready, /live, and /api/v1/health/workload."""

import os
import time
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.governance import Action, Resource, audit_event, require_policy
from app.models import User
from app.workload import assess_workload

router = APIRouter(tags=["health"])
log = structlog.get_logger(__name__)

_BOOT_TIME = time.time()


@router.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe — returns 200 if the process is running."""
    return {
        "status": "ok",
        "service": "football-iq-backend",
        "environment": os.environ.get("ENVIRONMENT", "development"),
        "uptime_seconds": round(time.time() - _BOOT_TIME, 1),
    }


@router.get("/live")
async def live() -> dict[str, str]:
    """Lightweight liveness probe (alias for container orchestrators)."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, object]:
    """Readiness probe — returns 200 only when critical dependencies are reachable."""
    checks: dict[str, str] = {}
    all_ok = True

    # Database check
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.error("readiness_check_failed", component="database", error=str(exc))
        checks["database"] = "unavailable"
        all_ok = False

    if not all_ok:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail={"status": "not_ready", "checks": checks})

    return {"status": "ready", "checks": checks}


@router.get("/api/v1/health/workload")
async def health_workload(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user: Annotated[User, Depends(require_policy(Resource.HEALTH_WORKLOAD, Action.READ))],
) -> dict[str, object]:
    """Return the current workload snapshot used by the gating layer.

    Restricted to sportsperformance / analyst / admin roles (see
    :data:`app.governance.POLICY`).  The payload contains only aggregate job
    counts and thresholds — never per-player health, names, or other PII.
    """
    snapshot = await assess_workload(db)
    audit_event(
        "audit.health_workload.read",
        actor_id=user.id,
        actor_role=user.role.value,
        resource=Resource.HEALTH_WORKLOAD.value,
        action=Action.READ.value,
    )
    return snapshot.to_dict()
