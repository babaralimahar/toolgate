"""End-to-end through HTTP with the real database: readiness, real API keys, the queue."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.adapters.db.api_keys import SqlApiKeyRepo
from app.main import create_app
from tests.conftest import make_settings


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        make_settings(database_url=str(engine.url.render_as_string(hide_password=False)))
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await app.state.engine.dispose()


async def make_key(sm: async_sessionmaker[AsyncSession], role: str) -> str:
    async with sm() as s:
        _, raw = await SqlApiKeyRepo(s).create(f"test-{role}", role)  # type: ignore[arg-type]
    return raw


async def test_readyz_checks_database(client: httpx.AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"


async def test_readyz_503_when_database_down() -> None:
    app = create_app(make_settings(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/none"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json()["type"].endswith("unavailable")
    await app.state.engine.dispose()


async def test_real_keys_and_job_roundtrip(
    client: httpx.AsyncClient, sm: async_sessionmaker[AsyncSession]
) -> None:
    clerk, viewer = await make_key(sm, "clerk"), await make_key(sm, "viewer")

    r = await client.post("/v1/jobs", headers={"X-API-Key": viewer}, json={"kind": "echo"})
    assert r.status_code == 403

    body = {"kind": "echo", "payload": {"a": 1}, "idempotency_key": "demo-key-0001"}
    r1 = await client.post("/v1/jobs", headers={"X-API-Key": clerk}, json=body)
    r2 = await client.post("/v1/jobs", headers={"X-API-Key": clerk}, json=body)
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["id"] == r2.json()["id"]
    assert (r1.json()["created"], r2.json()["created"]) == (True, False)

    r = await client.get(f"/v1/jobs/{r1.json()['id']}", headers={"X-API-Key": viewer})
    assert r.status_code == 200 and r.json()["status"] == "queued"

    missing = "00000000-0000-0000-0000-000000000000"
    r = await client.get(f"/v1/jobs/{missing}", headers={"X-API-Key": viewer})
    assert r.status_code == 404


async def test_disabled_key_rejected(
    client: httpx.AsyncClient, sm: async_sessionmaker[AsyncSession]
) -> None:
    async with sm() as s:
        key_id, raw = await SqlApiKeyRepo(s).create("tmp", "viewer")
    assert (await client.get("/v1/whoami", headers={"X-API-Key": raw})).status_code == 200
    async with sm() as s:
        assert await SqlApiKeyRepo(s).disable(key_id)
    assert (await client.get("/v1/whoami", headers={"X-API-Key": raw})).status_code == 401
