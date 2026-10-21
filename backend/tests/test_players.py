"""Tests for the players router (Issue #103)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import Player, User, UserRole
from fastapi.testclient import TestClient


def _make_user() -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = UserRole.coach
    u.is_active = True
    return u


def _make_player(
    *,
    player_id: uuid.UUID | None = None,
    first_name: str = "Marcus",
    last_name: str = "Carter",
    jersey_number: int | None = 11,
    position: str | None = "WR",
    position_group: str | None = "Skill",
    is_active: bool = True,
) -> Player:
    p = MagicMock(spec=Player)
    p.id = player_id or uuid.uuid4()
    p.first_name = first_name
    p.last_name = last_name
    p.jersey_number = jersey_number
    p.position = position
    p.position_group = position_group
    p.is_active = is_active
    p.user_id = None
    p.metadata_ = None
    p.created_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    return p


def _override_with_players(
    players: list[Player],
    *,
    captured_sql: dict[str, str] | None = None,
    detail_player: Player | None = None,
) -> None:
    """Wire dependency overrides so list queries return ``players`` and the
    detail query returns ``detail_player`` (or None to force 404)."""

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        calls = {"n": 0}

        async def _execute(stmt: Any) -> Any:
            calls["n"] += 1
            if captured_sql is not None:
                captured_sql["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": False}))
            result = MagicMock()
            # Detail endpoints call scalar_one_or_none; list endpoints call
            # scalars().all().  Configure both so either path works.
            result.scalar_one_or_none.return_value = detail_player
            scalars = MagicMock()
            scalars.all.return_value = players
            result.scalars.return_value = scalars
            return result

        session.execute = AsyncMock(side_effect=_execute)
        yield session

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _db


def test_list_players_returns_serialized_roster() -> None:
    p1 = _make_player(
        first_name="Marcus",
        last_name="Carter",
        jersey_number=11,
        position="WR",
        position_group="Skill",
    )
    p2 = _make_player(
        first_name="Drew",
        last_name="Evans",
        jersey_number=54,
        position="C",
        position_group="OL",
    )
    _override_with_players([p1, p2])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/players")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert body[0]["first_name"] == "Marcus"
    assert body[0]["jersey_number"] == 11
    assert body[0]["position"] == "WR"
    assert body[0]["position_group"] == "Skill"
    assert body[0]["is_active"] is True
    assert "created_at" in body[0]


def test_list_players_applies_position_and_active_filters() -> None:
    captured: dict[str, str] = {}
    _override_with_players([], captured_sql=captured)
    try:
        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/players",
                params={
                    "position": "WR",
                    "position_group": "Skill",
                    "is_active": "true",
                    "jersey_number": 11,
                    "search": "carter",
                    "limit": 25,
                    "offset": 5,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    sql = captured["sql"]
    # Each filter must reach the SQL layer — otherwise the API would silently
    # ignore caller intent.
    assert "players.position =" in sql
    assert "upper(trim(players.position_group)) =" in sql
    assert "players.is_active =" in sql
    assert "players.jersey_number =" in sql
    assert "lower(players.first_name)" in sql or "ILIKE" in sql.upper()
    assert "ORDER BY players.jersey_number" in sql
    assert "LIMIT" in sql.upper()


def test_get_player_returns_404_when_missing() -> None:
    _override_with_players([], detail_player=None)
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/players/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Player not found"


def test_get_player_returns_player_detail() -> None:
    pid = uuid.uuid4()
    player = _make_player(
        player_id=pid,
        first_name="Adam",
        last_name="Moore",
        jersey_number=9,
        position="QB",
        position_group="QB",
    )
    _override_with_players([], detail_player=player)
    try:
        with TestClient(app) as c:
            resp = c.get(f"/api/v1/players/{pid}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(pid)
    assert body["first_name"] == "Adam"
    assert body["last_name"] == "Moore"
    assert body["jersey_number"] == 9
    assert body["position"] == "QB"
    assert body["position_group"] == "QB"


def test_get_player_rejects_invalid_uuid() -> None:
    # No DB override needed — FastAPI's path validation must reject the input
    # before the dependency tree is invoked.
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/players/not-a-uuid")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
