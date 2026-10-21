"""Shared FastAPI dependencies — auth, database, role checks."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user_id
from app.database import get_db
from app.models import User, UserRole

log = structlog.get_logger(__name__)


async def get_current_user(
    user_id: Annotated[str, Depends(get_current_user_id)],
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> User:
    """Resolve the authenticated user from the JWT subject."""
    from sqlalchemy import select

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_roles(*roles: UserRole) -> Callable[..., Coroutine[Any, Any, "User"]]:
    """Return a FastAPI dependency that enforces one of the given roles."""

    async def _check(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not permitted for this action",
            )
        return user

    return _check


# Convenience role guards
require_admin = require_roles(UserRole.admin)
require_analyst_or_above = require_roles(UserRole.admin, UserRole.analyst)
require_coach_or_above = require_roles(UserRole.admin, UserRole.analyst, UserRole.coach)

# Sports-performance is a *parallel* track to coach, not a superset:
# admins/analysts see everything, sportsperformance staff own injury-risk and
# load-management views (e.g. pose asymmetry), and position coaches own
# tactical views.  Coach is intentionally excluded from this guard so coaches
# cannot retrieve sportsperformance-only signals; if you need both groups, use
# ``require_any_staff`` instead.
require_sportsperformance_or_above = require_roles(
    UserRole.admin,
    UserRole.analyst,
    UserRole.sportsperformance,
)
require_any_staff = require_roles(
    UserRole.admin,
    UserRole.analyst,
    UserRole.coach,
    UserRole.sportsperformance,
)
