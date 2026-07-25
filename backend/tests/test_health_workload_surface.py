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
from unittest.mock import MagicMock

import pytest
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
    try:
        with TestClient(app) as c:
            resp = c.get(SURFACE_URL)
    finally:
        app.dependency_overrides.clear()
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


@pytest.mark.parametrize("role", DENIED)
def test_surface_denied_for_unapproved_roles(role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    try:
        with TestClient(app) as c:
            resp = c.get(SURFACE_URL)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "policy_denied"
