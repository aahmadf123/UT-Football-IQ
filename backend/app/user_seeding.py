"""User seeding utilities for startup bootstrap and ops scripts.

Creates configured auth users idempotently and can optionally rotate their
passwords when an operator explicitly enables reset mode.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import hash_password
from app.models import User, UserRole

log = structlog.get_logger(__name__)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


async def _ensure_user(
    session: AsyncSession,
    email: str,
    password: str,
    full_name: str,
    role: UserRole,
    *,
    reset_passwords: bool,
) -> str:
    """Ensure one user exists; returns created|updated|unchanged."""
    email = _normalize_email(email)
    existing = await session.execute(select(User).where(func.lower(User.email) == email))
    row = existing.scalar_one_or_none()
    if row is not None:
        updated = False
        if row.role != role:
            row.role = role
            updated = True
        if reset_passwords:
            row.hashed_password = hash_password(password)
            updated = True
        if updated:
            log.info(
                "seed_user_updated", email=email, role=role.value, password_reset=reset_passwords
            )
            return "updated"
        log.info("seed_user_exists", email=email)
        return "unchanged"

    session.add(
        User(
            id=uuid.uuid4(),
            email=email,
            hashed_password=hash_password(password),
            full_name=full_name,
            role=role,
        )
    )
    log.info("seed_user_created", email=email, role=role.value)
    return "created"


async def seed_configured_users(
    *,
    database_url: str,
    admin_email: str,
    admin_password: str,
    worker_email: str,
    worker_password: str,
    reset_passwords: bool,
) -> dict[str, int]:
    """Seed configured users and return counts by action."""
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    stats = {"created": 0, "updated": 0, "unchanged": 0}

    try:
        async with session_factory() as session:
            if admin_email:
                outcome = await _ensure_user(
                    session,
                    admin_email,
                    admin_password,
                    "Administrator",
                    UserRole.admin,
                    reset_passwords=reset_passwords,
                )
                stats[outcome] += 1
            if worker_email:
                outcome = await _ensure_user(
                    session,
                    worker_email,
                    worker_password,
                    "GPU Worker",
                    UserRole.analyst,
                    reset_passwords=reset_passwords,
                )
                stats[outcome] += 1
            await session.commit()
    finally:
        await engine.dispose()

    return stats


def parse_reset_passwords_flag(value: str | None) -> bool:
    """Shared parser for reset toggle env vars."""
    return _truthy(value)
