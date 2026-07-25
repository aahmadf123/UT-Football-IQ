"""Auth router — register, login, token refresh, and user administration.

Registration is locked down: the requested role is **ignored**. The first
account ever created becomes ``admin`` (bootstrap); every later signup is a
``viewer`` until an admin promotes it via ``PATCH /users/{id}/role``. This
closes the self-serve privilege-escalation hole where a client-supplied
``role: "admin"`` was honoured verbatim.
"""

import os
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.deps import require_admin
from app.models import User, UserRole

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _normalize_email(email: str) -> str:
    """Case-fold emails so sign-up and sign-in always match.

    ``EmailStr`` lowercases the domain but preserves the local part, so
    registering ``Coach@utoledo.edu`` and later signing in as
    ``coach@utoledo.edu`` would otherwise miss and 401. Store and look up a
    single canonical (lowercased) form everywhere.
    """
    return email.strip().lower()


# ── Schemas ───────────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    email: EmailStr
    # Min 8 mirrors the frontend input. A too-short password now returns a
    # clear 422 instead of being silently accepted.
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1)
    # Accepted for wire compatibility but IGNORED — roles are assigned
    # server-side (first user = admin, everyone else = viewer).
    role: UserRole = UserRole.viewer

    @field_validator("password")
    @classmethod
    def _within_bcrypt_limit(cls, value: str) -> str:
        # bcrypt only hashes the first 72 bytes. REJECT longer passwords at
        # registration rather than silently truncating, so a user never
        # unknowingly sets a password whose tail is ignored (a multi-byte
        # password reaches 72 bytes in fewer than 72 characters).
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes long")
        return value


class RoleUpdateRequest(BaseModel):
    role: UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — not a secret, just the OAuth2 scheme name


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Create a new user account.

    The client-supplied ``role`` is deliberately ignored: the very first
    account bootstraps as ``admin``; all later accounts start as ``viewer``
    and are promoted by an admin.
    """
    email = _normalize_email(body.email)
    existing = await db.execute(select(User).where(func.lower(User.email) == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # Serialize the bootstrap decision: without this, two concurrent first
    # registrations both read count 0 and both become admin. The transaction-
    # scoped advisory lock releases on commit/rollback.
    await db.execute(text("SELECT pg_advisory_xact_lock(571_202_001)"))
    user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    assigned_role = UserRole.admin if user_count == 0 else UserRole.viewer

    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role=assigned_role,
    )
    db.add(user)
    try:
        # Commit here rather than leaving it to the get_db teardown (which runs
        # *after* the response is sent): the client's follow-up login/authed
        # request must find this row, and a same-email race that slipped past
        # the check above must surface as a clean 409, not a 500.
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc
    await db.refresh(user)
    log.info(
        "user_registered",
        user_id=str(user.id),
        email=user.email,
        role=assigned_role.value,
        bootstrap_admin=user_count == 0,
    )
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """Authenticate and return JWT access + refresh tokens."""
    normalized = _normalize_email(body.email)
    result = await db.execute(select(User).where(func.lower(User.email) == normalized))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        if user_count == 0:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No accounts exist yet. Use Create account to bootstrap the first admin.",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account inactive")

    extra = {"role": user.role.value}
    access = create_access_token(str(user.id), extra=extra)
    refresh = create_refresh_token(str(user.id))
    log.info("user_login", user_id=str(user.id))
    return TokenResponse(access_token=access, refresh_token=refresh)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """Exchange a refresh token for a new access token."""
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    user_id: str = payload.get("sub", "")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    extra = {"role": user.role.value}
    access = create_access_token(str(user.id), extra=extra)
    refresh = create_refresh_token(str(user.id))
    return TokenResponse(access_token=access, refresh_token=refresh)


# ── User administration (admin-only) ─────────────────────────────────────────


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
) -> list[User]:
    """List all user accounts (admin only)."""
    result = await db.execute(select(User).order_by(User.created_at))
    return list(result.scalars().all())


@router.patch("/users/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: uuid.UUID,
    body: RoleUpdateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
) -> User:
    """Change a user's role (admin only). The last admin cannot demote itself."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.role == UserRole.admin and body.role != UserRole.admin and user.id == admin.id:
        admin_count = (
            await db.execute(
                select(func.count()).select_from(User).where(User.role == UserRole.admin)
            )
        ).scalar_one()
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot demote the last remaining admin",
            )

    user.role = body.role
    await db.flush()
    log.info(
        "user_role_updated",
        user_id=str(user.id),
        new_role=body.role.value,
        updated_by=str(admin.id),
    )
    return user


# ── Development auto-login ───────────────────────────────────────────────────


@router.post("/dev-login", response_model=TokenResponse)
async def dev_login(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """Issue tokens for the seeded admin without credentials — dev only.

    Enabled only when ``ENVIRONMENT=development`` **and** ``DEV_AUTOLOGIN=1``;
    404s otherwise so the route is invisible in production.
    """
    settings = get_settings()
    autologin = os.environ.get("DEV_AUTOLOGIN", "").strip().lower() in {"1", "true", "yes", "on"}
    if settings.environment.strip().lower() != "development" or not autologin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    result = await db.execute(
        select(User).where(User.role == UserRole.admin, User.is_active.is_(True)).limit(1)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No admin user exists yet — register the first account",
        )
    extra = {"role": user.role.value}
    access = create_access_token(str(user.id), extra=extra)
    refresh = create_refresh_token(str(user.id))
    log.info("dev_login", user_id=str(user.id))
    return TokenResponse(access_token=access, refresh_token=refresh)
