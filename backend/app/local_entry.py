"""Local-only browser entry; hosted deployments continue using app.main."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.auth import create_access_token, create_refresh_token
from app.database import get_db
from app.main import app
from app.models import User, UserRole

_lock = threading.Lock()
# Only /local/bootstrap mints one-use nonces. Restarting the API never
# reconstructs a previously consumed browser token from its environment.
_tokens: dict[str, float] = {}
_launcher_key = os.environ.get("LOCAL_APP_LAUNCHER_KEY", "")


class LocalLogin(BaseModel):
    bootstrap_token: str


class LocalBootstrap(BaseModel):
    launcher_key: str


@app.post("/api/v1/local/bootstrap", include_in_schema=False)
async def local_bootstrap(body: LocalBootstrap, request: Request) -> dict[str, str]:
    client = request.client.host if request.client else ""
    if client not in {"127.0.0.1", "::1"} or not _launcher_key:
        raise HTTPException(status_code=404, detail="Not found")
    if not hmac.compare_digest(body.launcher_key, _launcher_key):
        raise HTTPException(status_code=401, detail="Local launcher authentication failed")
    with _lock:
        for expired in [
            value for value, deadline in _tokens.items() if time.monotonic() > deadline
        ]:
            _tokens.pop(expired)
        token = secrets.token_urlsafe(32)
        _tokens[token] = time.monotonic() + 300
    return {"bootstrap_token": token}


@app.post("/api/v1/local/login", include_in_schema=False)
async def local_login(
    body: LocalLogin,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> dict[str, str]:
    client = request.client.host if request.client else ""
    if client not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=404, detail="Not found")
    with _lock:
        deadline = _tokens.pop(body.bootstrap_token, 0)
        if not deadline or time.monotonic() > deadline:
            raise HTTPException(
                status_code=401, detail="Local sign-in expired. Relaunch the library."
            )
    result = await db.execute(
        select(User).where(User.role == UserRole.admin, User.is_active.is_(True)).limit(1)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=503, detail="The local administrator is not ready")
    return {
        "access_token": create_access_token(str(user.id), extra={"role": user.role.value}),
        "refresh_token": create_refresh_token(str(user.id)),
        "token_type": "bearer",
    }


class FilmRoomFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404 or path.startswith(("api/", "_next/")):
                raise
            # Next.js static export includes a dedicated 404 page; do not mask API failures.
            return FileResponse(Path(str(self.directory)) / "404.html", status_code=404)


_static = Path(
    os.environ.get("LOCAL_APP_STATIC_DIR", Path(__file__).resolve().parents[2] / "frontend" / "out")
)
if _static.is_dir():
    app.mount("/", FilmRoomFiles(directory=str(_static), html=True), name="film-room")
