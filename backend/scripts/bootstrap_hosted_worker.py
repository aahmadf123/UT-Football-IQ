"""Create a hosted service account without changing staff roles or passwords."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

from app.auth import hash_password, verify_password
from app.database import AsyncSessionLocal, engine
from app.models import User, UserRole
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_worker(db: AsyncSession, email: str, password: str) -> int:
    """Return 0 when ready; preserve first-admin signup and existing accounts."""
    try:
        email = str(TypeAdapter(EmailStr).validate_python(email)).strip().lower()
    except ValidationError:
        return 3
    if not password:
        return 3
    # The registration endpoint uses this same transaction-scoped lock.
    await db.execute(text("SELECT pg_advisory_xact_lock(571_202_001)"))
    admin = await db.scalar(
        select(User.id).where(User.role == UserRole.admin, User.is_active.is_(True)).limit(1)
    )
    if admin is None:
        return 2
    existing = await db.scalar(select(User).where(func.lower(User.email) == email))
    if existing is not None:
        if (
            not existing.is_active
            or existing.role
            not in {UserRole.admin, UserRole.analyst, UserRole.coach, UserRole.sportsperformance}
            or not verify_password(password, existing.hashed_password)
        ):
            return 4
        return 0
    db.add(
        User(
            id=uuid.uuid4(),
            email=email,
            full_name="Hosted CPU worker",
            hashed_password=hash_password(password),
            role=UserRole.analyst,
        )
    )
    await db.commit()
    return 0


async def main() -> int:
    try:
        async with AsyncSessionLocal() as db:
            result = await ensure_worker(
                db, os.environ.get("WORKER_EMAIL", ""), os.environ.get("WORKER_PASSWORD", "")
            )
        print(
            json.dumps({"event": "hosted_worker_bootstrap", "ready": result == 0, "code": result})
        )
        return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
