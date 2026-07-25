"""Athlete health/workload surface tests (Issue #113).

Covers the role-gated, audit-logged groundwork surface:

* ``app.health_workload.build_surface_status`` payload is policy-safe (no PII).
* ``GET /api/v1/health-workload/surface`` RBAC gate — approved roles get the
  surface, everyone else gets ``403 policy_denied``.
* The placeholder integration contracts (wellness, GPS/wearables, S&C) are all
  present and start ``not_connected``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.database import get_db
from app.deps import get_current_user
from app.health_workload import (
    INTEGRATION_CONTRACTS,
    IntegrationSource,
    IntegrationStatus,
    build_surface_status,
)
from app.main import app
from app.models import User, UserRole
from fastapi.testclient import TestClient

SURFACE_URL = "/api/v1/health-workload/surface"

# Roles allowed to read the surface vs. everyone else. Mirrors
# app.governance.POLICY[(HEALTH_WORKLOAD, READ)].
APPROVED = [UserRole.admin, UserRole.analyst, UserRole.sportsperformance]
DENIED = [UserRole.coach, UserRole.player, UserRole.viewer]


def _make_user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


class _CountingSession:
    """Minimal async-session stand-in returning a fixed row count.

    The surface route counts rows in the health-ingest tables to decide which
    integrations report ``connected``. These tests are about the RBAC gate and
    the payload shape, so the count is stubbed rather than requiring the whole
    health-fusion schema to exist.
    """

    def __init__(self, count: int) -> None:
        self._count = count

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._count
        return result


def _override_db(count: int) -> None:
    async def _session() -> AsyncGenerator[Any, None]:
        yield _CountingSession(count)

    app.dependency_overrides[get_db] = _session


@pytest.fixture(autouse=True)
def _clean_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


# ── Pure surface contract ────────────────────────────────────────────────────


def test_integration_contracts_cover_five_sources_and_start_disconnected() -> None:
    sources = {c.source for c in INTEGRATION_CONTRACTS}
    assert sources == {
        IntegrationSource.wellness,
        IntegrationSource.gps_wearables,
        IntegrationSource.strength_conditioning,
        IntegrationSource.academic_calendar,
        IntegrationSource.injury_history,
    }
    assert all(c.status == IntegrationStatus.not_connected for c in INTEGRATION_CONTRACTS)


def test_build_surface_status_is_policy_safe() -> None:
    payload = build_surface_status(role=UserRole.sportsperformance)
    assert payload["data_available"] is False
    assert payload["role"] == "sportsperformance"
    # Non-medical disclaimer is present and explicit.
    disclaimer = payload["disclaimer"].lower()
    assert "not a medical device" in disclaimer
    assert "predict injury" in disclaimer
    # Approved-role list is derived from the central policy.
    assert set(payload["approved_roles"]) == {"admin", "analyst", "sportsperformance"}
    # The surface never carries athlete PII / data keys.
    forbidden = {"player_id", "players", "name", "athlete", "metrics", "health"}
    assert forbidden.isdisjoint(payload.keys())
    assert len(payload["integrations"]) == 5


def test_build_surface_status_flips_connected_from_source_counts() -> None:
    payload = build_surface_status(
        role=UserRole.sportsperformance,
        source_counts={"wellness": 12, "gps_wearables": 0},
    )
    by_source = {i["source"]: i["status"] for i in payload["integrations"]}
    assert by_source["wellness"] == "connected"
    assert by_source["gps_wearables"] == "not_connected"
    assert payload["data_available"] is True


# ── RBAC gate ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", APPROVED)
def test_surface_allowed_for_approved_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db(count=0)
    with TestClient(app) as c:
        resp = c.get(SURFACE_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_available"] is False
    assert body["role"] == role.value
    assert {i["source"] for i in body["integrations"]} == {
        "wellness",
        "gps_wearables",
        "strength_conditioning",
        "academic_calendar",
        "injury_history",
    }
    assert all(i["status"] == "not_connected" for i in body["integrations"])


def test_surface_reports_connected_once_rows_exist() -> None:
    """The regression this endpoint used to have.

    ``build_surface_status`` was called without ``source_counts``, so every
    integration reported ``not_connected`` forever — the UI insisted nothing was
    hooked up while the dashboard beside it served data from those same tables.
    """
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db(count=7)
    with TestClient(app) as c:
        resp = c.get(SURFACE_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data_available"] is True
    by_source = {i["source"]: i["status"] for i in body["integrations"]}
    for source in ("wellness", "gps_wearables", "strength_conditioning"):
        assert by_source[source] == "connected"


def test_surface_carries_no_athlete_data_even_when_connected() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(UserRole.sportsperformance)
    _override_db(count=42)
    with TestClient(app) as c:
        body = c.get(SURFACE_URL).json()
    # Counts prove a feed is live; they must never become a data channel.
    forbidden = {"player_id", "players", "name", "athlete", "metrics", "health", "source_counts"}
    assert forbidden.isdisjoint(body.keys())


@pytest.mark.parametrize("role", DENIED)
def test_surface_denied_for_unapproved_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    _override_db(count=0)
    with TestClient(app) as c:
        resp = c.get(SURFACE_URL)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"
