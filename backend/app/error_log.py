"""Recent unhandled API errors, kept in memory for admins.

The API runs in a container whose stdout is awkward to reach, so an
unexpected exception is otherwise only visible as a bare 500. Each one is
recorded here with an id that is also returned to the client, and admins
read the list from ``GET /api/v1/system/errors``.
"""

from __future__ import annotations

import re
import traceback
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

log = structlog.get_logger("app.errors")

_RECENT: deque[dict[str, Any]] = deque(maxlen=50)
#: Credentials embedded in connection strings, should one appear in a message.
_URL_CREDENTIALS = re.compile(r"://[^/\s@]+@")


def _location(exc: BaseException) -> str | None:
    """The innermost frame outside installed packages, as ``app/x.py:123 in func``."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "site-packages" not in f.filename]
    if not (ours or frames):
        return None
    frame = (ours or frames)[-1]
    path = frame.filename.replace("\\", "/")
    for root in ("/app/", "/tests/"):
        if root in path:
            path = path[path.rfind(root) + 1 :]
            break
    else:
        path = path.rsplit("/", 1)[-1]
    return f"{path}:{frame.lineno} in {frame.name}"


def record(request: Request, exc: BaseException) -> str:
    """Remember an unhandled exception; returns its error id."""
    error_id = uuid.uuid4().hex[:12]
    entry = {
        "error_id": error_id,
        "at": datetime.now(UTC).isoformat(),
        "method": request.method,
        "path": request.url.path,
        "error_type": type(exc).__name__,
        "location": _location(exc),
        "message": _URL_CREDENTIALS.sub("://***@", str(exc))[:500],
    }
    _RECENT.appendleft(entry)
    log.error("unhandled_exception", **entry)
    return error_id


def recent() -> list[dict[str, Any]]:
    return list(_RECENT)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """JSON 500 with an error id instead of Starlette's plain-text page."""
    error_id = record(request, exc)
    return JSONResponse(
        {"detail": "Something went wrong on the server.", "error_id": error_id},
        status_code=500,
    )
