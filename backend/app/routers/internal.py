"""Internal routes for an external scheduler.

Not part of the public API and not exposed to any browser: authentication is a
static bearer token shared with whatever drives the schedule (a Cloudflare cron
trigger, a Kubernetes CronJob, a systemd timer), never a user JWT.

Why this exists: the nightly work used to be an asyncio loop on the FastAPI
lifespan, which only fires while a process is alive. On a platform that suspends
an idle container -- Cloudflare Containers sleep after ~20 minutes of quiet --
that process simply is not running at 08:00 UTC, so the tick never happens and
the correction-to-training flywheel silently stops turning. Moving the *trigger*
outside the container fixes that without moving the work.

The endpoint is disabled (404) unless ``SCHEDULER_TOKEN`` is set. A tick route
reachable without a credential is worse than no tick route.
"""

from __future__ import annotations

import hmac
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.scheduler import run_locked_tick

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def _require_scheduler_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Authenticate with the shared scheduler token.

    404 rather than 401 when unconfigured: an operator who has not set the token
    has not opted into this surface, and it should not advertise itself.
    """
    token = get_settings().scheduler_token
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    presented = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()

    # compare_digest on the encoded values: it is constant-time in the content,
    # and encoding first avoids the unicode path that raises on non-ASCII.
    if not presented or not hmac.compare_digest(presented.encode(), token.encode()):
        log.warning("internal_auth_failed", route="scheduler_tick")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid scheduler token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post("/scheduler/tick", dependencies=[Depends(_require_scheduler_token)])
async def scheduler_tick(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, object]:
    """Run the nightly tick now.

    Safe to call repeatedly: a Postgres advisory lock admits one caller at a
    time, and every step of the tick is individually idempotent (per-day job
    checks, flag-driven correction export). A cron delivery that fires twice, or
    overlaps a still-running tick, costs nothing.

    Returns ``{"status": "skipped"}`` when another caller holds the lock -- that
    is a normal outcome, not a failure, so it is still a 200.
    """
    summary = await run_locked_tick(db)
    if summary is None:
        return {"status": "skipped", "reason": "another tick holds the lock"}
    return {"status": "ok", **summary}
