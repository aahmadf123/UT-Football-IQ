"""SSE alert stream router (Issue #16).

Endpoint:
  GET /api/v1/alerts/stream

Streams real-time coaching alerts to the coaching app on laptop/iPad using
Server-Sent Events (SSE).  Aligns with Cloudflare Workers edge streaming
architecture — no WebSocket infra needed for this one-way push.

SSE event format (per W3C EventSource spec):
    data: {"alert_type": "effort_anomaly", "player_id": "...", ...}\n\n

Keep-alive events are emitted every 25 seconds to maintain the connection
through load balancers and proxy timeouts.

Pub/sub mechanism: an in-process asyncio.Queue registry keyed by connection
UUID.  The ``publish_alert`` helper (called from the alerts REST router or
the GPU worker webhook) fans the alert dict to all registered queues whose
position_group filter matches.

Player and viewer roles are blocked — alerts are coaching-staff only.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.deps import get_current_user
from app.models import AlertType, User, UserRole

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/alerts", tags=["alerts-sse"])

# ── Restricted alert types (Issue #149 / #9) ─────────────────────────────────
# Workload-risk alerts are sports-performance context, not coaching tactics:
# they are visible ONLY to admin + sportsperformance. Coaches and analysts
# never see them in the list, by ID, or over SSE — fatigue/injury-risk flags
# must not reach coaching staff without sports-performance mediation. Defined
# here (not in the REST router) because the REST router imports this module.
RESTRICTED_ALERT_TYPES: frozenset[AlertType] = frozenset({AlertType.workload_risk})
WORKLOAD_ALERT_ROLES: frozenset[UserRole] = frozenset({UserRole.admin, UserRole.sportsperformance})


def alert_type_visible_to(role: UserRole, alert_type: AlertType) -> bool:
    """Whether ``role`` may see alerts of ``alert_type`` at all."""
    if alert_type in RESTRICTED_ALERT_TYPES:
        return role in WORKLOAD_ALERT_ROLES
    return role not in (UserRole.player, UserRole.viewer)


def _alert_dict_visible_to(role: UserRole | None, alert_type_value: Any) -> bool:
    """Visibility check for a serialized alert dict (SSE fan-out path).

    Unknown/missing roles are denied for restricted types — default deny.
    """
    try:
        alert_type = AlertType(str(alert_type_value))
    except ValueError:
        return True  # unknown type: not restricted
    if alert_type not in RESTRICTED_ALERT_TYPES:
        return True
    return role is not None and role in WORKLOAD_ALERT_ROLES


# ── In-process pub/sub registry ───────────────────────────────────────────────
# Maps connection_id → (queue, position_group_filter, role)
# position_group_filter=None means "receive all groups" (admin/analyst)
_connections: dict[str, tuple[asyncio.Queue[dict[str, Any]], str | None, UserRole | None]] = {}

_KEEPALIVE_SECONDS = 25
_QUEUE_MAX_SIZE = 200


def publish_alert(alert_dict: dict[str, Any]) -> None:
    """Fan an alert dict to all connected SSE clients whose filter matches.

    Called by the alerts REST router after persisting the alert, or directly
    by the GPU worker via the backend publish endpoint. Restricted alert
    types (workload_risk) are only fanned to sports-performance/admin
    connections regardless of position-group filters.
    """
    pg = alert_dict.get("position_group")
    alert_type_value = alert_dict.get("alert_type")
    fanned = 0
    for _conn_id, (q, conn_pg, conn_role) in list(_connections.items()):
        if not _alert_dict_visible_to(conn_role, alert_type_value):
            continue
        if conn_pg is not None and pg and conn_pg.upper() != str(pg).upper():
            continue
        if not q.full():
            q.put_nowait(alert_dict)
            fanned += 1
    log.debug("alert_published_to_sse", alert_type=alert_type_value, connections=fanned)


# ── SSE generator ─────────────────────────────────────────────────────────────


async def _sse_generator(
    conn_id: str,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted strings for the lifetime of the connection."""
    queue, _, _ = _connections[conn_id]
    try:
        yield f"data: {json.dumps({'type': 'connected', 'connection_id': conn_id})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                yield f"data: {json.dumps(event)}\n\n"
            except TimeoutError:
                yield 'data: {"type": "keepalive"}\n\n'
    finally:
        _connections.pop(conn_id, None)
        log.info("sse_client_disconnected", conn_id=conn_id)


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/stream")
async def stream_alerts(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    position_group: str | None = Query(
        default=None,
        description="Filter stream to a specific position group. Admins/analysts may omit.",
    ),
) -> StreamingResponse:
    """Open an SSE connection to receive real-time coaching alerts.

    Players and viewers are blocked.  Coaches default to their assigned
    position group; admins and analysts may receive all groups.

    The stream emits:
      - ``connected`` event on open
      - ``keepalive`` events every 25 s
      - alert JSON objects as they are published

    Client usage (JavaScript):
        const es = new EventSource('/api/v1/alerts/stream');
        es.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    if current_user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Alert stream is not available in player- or viewer-facing views",
        )

    # Determine effective position group filter
    user_pg = getattr(current_user, "position_group", None)
    if current_user.role in (UserRole.admin, UserRole.analyst):
        effective_pg: str | None = position_group.upper() if position_group else None
    else:
        if user_pg is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Position group access is restricted to your assigned group",
            )
        if position_group and position_group.upper() != user_pg.upper():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Position group override is not allowed for your role",
            )
        effective_pg = user_pg.upper()

    conn_id = str(uuid.uuid4())
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
    _connections[conn_id] = (queue, effective_pg, current_user.role)

    log.info(
        "sse_client_connected",
        conn_id=conn_id,
        user_id=str(current_user.id),
        position_group=effective_pg,
    )

    return StreamingResponse(
        _sse_generator(conn_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
