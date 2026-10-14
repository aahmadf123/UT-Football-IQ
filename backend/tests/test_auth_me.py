"""The native bootstrap token can resolve its user through the normal auth API."""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

from app.auth import create_access_token
from app.database import get_db
from app.main import app
from app.models import User, UserRole
from fastapi.testclient import TestClient


def test_me_resolves_signed_access_token_and_omits_password() -> None:
    user = User(
        id=uuid.uuid4(),
        email="native@test.local",
        full_name="Local coach",
        role=UserRole.admin,
        is_active=True,
        hashed_password="private-hash",
    )

    async def db_override() -> AsyncGenerator[AsyncMock, None]:
        result = MagicMock()
        result.scalar_one_or_none.return_value = user
        db = AsyncMock()
        db.execute.return_value = result
        yield db

    app.dependency_overrides[get_db] = db_override
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {create_access_token(str(user.id))}"},
            )
        assert response.status_code == 200
        assert response.json() == {
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": "admin",
            "is_active": True,
        }
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_me_rejects_anonymous_and_invalid_tokens() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/me").status_code == 401
        assert (
            client.get("/api/v1/auth/me", headers={"Authorization": "Bearer invalid"}).status_code
            == 401
        )
