"""One-use loopback browser sign-in is minted afresh after every API start."""

from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from app import local_entry
from app.database import get_db
from app.models import UserRole


@pytest_asyncio.fixture
async def local_browser(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(local_entry, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(local_entry, "_tokens", {})
    monkeypatch.setattr(local_entry, "_launcher_key", "test-private-launcher-key")
    user = SimpleNamespace(id=uuid.uuid4(), role=UserRole.admin)

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: user)

    async def session():
        yield Session()

    local_entry.app.dependency_overrides[get_db] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=local_entry.app, client=("127.0.0.1", 1234)),
        base_url="http://localhost",
    ) as client:
        yield client, clock
    local_entry.app.dependency_overrides.pop(get_db, None)


async def mint(client):
    response = await client.post(
        "/api/v1/local/bootstrap", json={"launcher_key": "test-private-launcher-key"}
    )
    assert response.status_code == 200
    return response.json()["bootstrap_token"]


async def test_bootstrap_nonce_is_consumed_once_and_reopen_mints_new_value(local_browser):
    client, _ = local_browser
    first = await mint(client)
    signed_in = await client.post("/api/v1/local/login", json={"bootstrap_token": first})
    assert signed_in.status_code == 200
    assert signed_in.json()["token_type"] == "bearer"
    assert "access_token" in signed_in.json()
    replay = await client.post("/api/v1/local/login", json={"bootstrap_token": first})
    assert replay.status_code == 401
    second = await mint(client)
    assert first != second
    assert (
        await client.post("/api/v1/local/login", json={"bootstrap_token": second})
    ).status_code == 200


async def test_bootstrap_nonce_expires_after_five_minutes(local_browser):
    client, clock = local_browser
    nonce = await mint(client)
    clock[0] += 301
    expired = await client.post("/api/v1/local/login", json={"bootstrap_token": nonce})
    assert expired.status_code == 401
    assert nonce not in local_entry._tokens


async def test_bootstrap_requires_the_private_launcher_key(local_browser):
    client, _ = local_browser
    response = await client.post("/api/v1/local/bootstrap", json={"launcher_key": "wrong"})
    assert response.status_code == 401
    assert not local_entry._tokens


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/local/bootstrap", {"launcher_key": "test-private-launcher-key"}),
        ("/api/v1/local/login", {"bootstrap_token": "invalid"}),
    ],
)
async def test_local_sign_in_endpoints_refuse_non_loopback_clients(local_browser, path, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=local_entry.app, client=("192.0.2.100", 1234)),
        base_url="http://localhost",
    ) as client:
        response = await client.post(path, json=body)
    assert response.status_code == 404


async def test_api_reload_never_restores_environment_browser_nonce(local_browser, monkeypatch):
    client, _ = local_browser
    nonce = await mint(client)
    assert (
        await client.post("/api/v1/local/login", json={"bootstrap_token": nonce})
    ).status_code == 200
    monkeypatch.setenv("LOCAL_APP_BOOTSTRAP_TOKEN", nonce)
    # A new API process starts with an empty token registry, even if an older
    # launcher left a consumed nonce in its environment.
    importlib.reload(local_entry)
    assert not local_entry._tokens
    replay = await client.post("/api/v1/local/login", json={"bootstrap_token": nonce})
    assert replay.status_code == 401
