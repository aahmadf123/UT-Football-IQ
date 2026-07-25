"""Registration lockdown + user administration tests (real Postgres).

Covers the closed privilege-escalation hole: the first account bootstraps as
admin, later signups are viewers regardless of the requested role, role
changes are admin-only, and the last admin cannot demote itself.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from app.config import get_settings
from app.database import Base
from app.models import User, UserRole
from app.routers.auth import (
    LoginRequest,
    RegisterRequest,
    RoleUpdateRequest,
    dev_login,
    login,
    register,
    update_user_role,
)
from fastapi import HTTPException
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_USERS = Base.metadata.tables["users"]


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        existing: set[str] = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    created = _USERS.name not in existing
    if created:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[_USERS]))
    else:
        async with engine.begin() as conn:
            await conn.exec_driver_sql('TRUNCATE TABLE "users" CASCADE')
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if created:
            async with engine.begin() as conn:
                await conn.exec_driver_sql('DROP TABLE IF EXISTS "users" CASCADE')
            try:
                async with engine.begin() as conn:
                    await conn.exec_driver_sql("DROP TYPE IF EXISTS user_role")
            except Exception:
                pass
        else:
            async with engine.begin() as conn:
                await conn.exec_driver_sql('TRUNCATE TABLE "users" CASCADE')
        await engine.dispose()


def _register_request(email: str, role: UserRole = UserRole.viewer) -> RegisterRequest:
    return RegisterRequest(email=email, password="pw-123456", full_name="T User", role=role)


async def test_first_user_bootstraps_admin_then_viewers(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        first = await register(_register_request("first@example.com"), session)
        await session.commit()
    assert first.role == UserRole.admin

    # Second signup REQUESTS admin — must be ignored.
    async with db() as session:
        second = await register(
            _register_request("second@example.com", role=UserRole.admin), session
        )
        await session.commit()
    assert second.role == UserRole.viewer


async def test_email_case_insensitive_signup_and_signin(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Register with a mixed-case email; signing in with a different case must
    # still match (emails are normalized to lowercase at both ends).
    from app.routers.auth import LoginRequest, login

    async with db() as session:
        await register(_register_request("Mixed.Case@Example.COM"), session)

    async with db() as session:
        tokens = await login(
            LoginRequest(email="mixed.case@example.com", password="pw-123456"), session
        )
    assert tokens.access_token and tokens.refresh_token


async def test_duplicate_registration_conflicts_case_insensitively(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        await register(_register_request("dupe@example.com"), session)

    async with db() as session:
        with pytest.raises(HTTPException) as exc:
            # Same address, different case — must be rejected as a duplicate.
            await register(_register_request("Dupe@Example.com"), session)
    assert exc.value.status_code == 409


async def test_login_guides_bootstrap_when_no_users_exist(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        with pytest.raises(HTTPException) as exc:
            await login(LoginRequest(email="admin@example.com", password="pw-123456"), session)
    assert exc.value.status_code == 401
    assert "No accounts exist yet" in str(exc.value.detail)


async def test_admin_can_promote_and_last_admin_protected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        admin = await register(_register_request("boss@example.com"), session)
        viewer = await register(_register_request("member@example.com"), session)
        await session.commit()
        admin_id, viewer_id = admin.id, viewer.id

    async with db() as session:
        admin_row = (await session.execute(select(User).where(User.id == admin_id))).scalar_one()
        promoted = await update_user_role(
            viewer_id, RoleUpdateRequest(role=UserRole.coach), session, admin_row
        )
        await session.commit()
    assert promoted.role == UserRole.coach

    # The only admin demoting itself is refused.
    async with db() as session:
        admin_row = (await session.execute(select(User).where(User.id == admin_id))).scalar_one()
        with pytest.raises(HTTPException) as exc:
            await update_user_role(
                admin_id, RoleUpdateRequest(role=UserRole.viewer), session, admin_row
            )
    assert exc.value.status_code == 409


async def test_role_update_missing_user_404(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        admin = await register(_register_request("solo@example.com"), session)
        await session.commit()
        with pytest.raises(HTTPException) as exc:
            await update_user_role(
                uuid.uuid4(), RoleUpdateRequest(role=UserRole.coach), session, admin
            )
    assert exc.value.status_code == 404


async def test_dev_login_disabled_outside_development(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # ENVIRONMENT=test in the suite → the route must 404 even with the flag.
    async with db() as session:
        with pytest.raises(HTTPException) as exc:
            await dev_login(session)
    assert exc.value.status_code == 404


async def test_dev_login_returns_admin_tokens_in_development(
    db: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "environment", "development")
    monkeypatch.setenv("DEV_AUTOLOGIN", "1")
    async with db() as session:
        await register(_register_request("root@example.com"), session)
        await session.commit()
        tokens = await dev_login(session)
    assert tokens.access_token and tokens.refresh_token
