"""Hosted inference bootstrap must preserve staff authorization decisions."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models import UserRole
from scripts.bootstrap_hosted_worker import ensure_worker


async def test_first_admin_signup_remains_available() -> None:
    db = AsyncMock()
    db.scalar.return_value = None
    assert await ensure_worker(db, "worker@example.com", "fixture-password") == 2
    db.commit.assert_not_awaited()


async def test_existing_staff_roles_and_passwords_are_preserved() -> None:
    existing = MagicMock(is_active=True, role=UserRole.admin, hashed_password="fixture-hash")
    db = AsyncMock()
    db.scalar.side_effect = [uuid.uuid4(), existing]
    with patch("scripts.bootstrap_hosted_worker.verify_password", return_value=True):
        assert await ensure_worker(db, "worker@example.com", "fixture-password") == 0
    assert existing.role == UserRole.admin
    assert existing.hashed_password == "fixture-hash"
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.viewer, UserRole.player])
async def test_bootstrap_cannot_promote_nonstaff_into_worker(role: UserRole) -> None:
    db = AsyncMock()
    db.scalar.side_effect = [uuid.uuid4(), MagicMock(is_active=True, role=role)]
    assert await ensure_worker(db, "worker@example.com", "fixture-password") == 4
    db.commit.assert_not_awaited()
