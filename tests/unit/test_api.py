"""HTTP behaviour without a database: auth is backed by an in-memory key repo."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.db.api_keys import ApiKeyRecord, hash_key
from app.api.auth import get_api_key_repo
from app.main import create_app
from app.observability.logging import configure_logging
from tests.conftest import make_settings

KEYS = {
    "sk_viewer_aaaaaaaaaaaaaaaaaaaa": ApiKeyRecord("key_viewer", "viewer", False),
    "sk_approver_bbbbbbbbbbbbbbbbbb": ApiKeyRecord("key_approver", "approver", False),
    "sk_admin_cccccccccccccccccccccc": ApiKeyRecord("key_admin", "admin", False),
    "sk_disabled_dddddddddddddddddd": ApiKeyRecord("key_disabled", "admin", True),
}
VIEWER, APPROVER, ADMIN, DISABLED = KEYS


class MemoryRepo:
    def __init__(self) -> None:
        self._by_hash = {hash_key(k): v for k, v in KEYS.items()}

    async def find_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        return self._by_hash.get(key_hash)


def build_app(**overrides: object) -> FastAPI:
    app = create_app(make_settings(**overrides))
    app.dependency_overrides[get_api_key_repo] = MemoryRepo

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail: db password is hunter2")

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def assert_problem(resp: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, object] = resp.json()
    assert body["status"] == status
    assert str(body["type"]).endswith(code)
    assert body["request_id"] == resp.headers["x-request-id"]
    return body


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_401_without_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/whoami")
    assert_problem(r, 401, "unauthenticated")
    assert "ApiKey" in r.headers["www-authenticate"]


async def test_401_with_unknown_key(client: httpx.AsyncClient) -> None:
    assert_problem(
        await client.get("/v1/whoami", headers={"X-API-Key": "sk_nope"}), 401, "unauthenticated"
    )


async def test_401_with_disabled_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/whoami", headers={"X-API-Key": DISABLED})
    assert_problem(r, 401, "unauthenticated")


async def test_200_with_valid_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/whoami", headers={"X-API-Key": VIEWER})
    assert r.status_code == 200 and r.json() == {"key_id": "key_viewer", "role": "viewer"}


async def test_403_wrong_role(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/complete", headers={"X-API-Key": VIEWER}, json={"prompt": "hi"})
    body = assert_problem(r, 403, "forbidden")
    assert body["detail"] == "requires approver"


async def test_higher_role_inherits_lower(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/whoami", headers={"X-API-Key": ADMIN})
    assert r.status_code == 200


async def test_auth_checked_before_body_validation(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/complete", json={"wrong": True})
    assert_problem(r, 401, "unauthenticated")


async def test_422_is_problem_json_without_input_values(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/complete", headers={"X-API-Key": APPROVER}, json={"prompt": "", "extra": "s3cret"}
    )
    body = assert_problem(r, 422, "validation-error")
    assert isinstance(body["errors"], list) and body["errors"]
    assert "s3cret" not in r.text


async def test_404_is_problem_json(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get("/does-not-exist"), 404, "not-found")


async def test_500_hides_internals(client: httpx.AsyncClient) -> None:
    r = await client.get("/boom")
    body = assert_problem(r, 500, "internal")
    assert "hunter2" not in r.text and "Traceback" not in r.text
    assert body["title"] == "Internal error"


async def test_request_id_generated_and_echoed(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.headers["x-request-id"].startswith("req_")
    r2 = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert r2.headers["x-request-id"] == "abc-123"


async def test_unsafe_incoming_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Request-ID": "bad id\nwith newline" * 10})
    assert r.headers["x-request-id"].startswith("req_")


async def test_llm_complete_with_fake_provider(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/complete", headers={"X-API-Key": APPROVER}, json={"prompt": "ping"})
    assert r.status_code == 200
    assert r.json()["text"] == "ping" and r.json()["model"] == "fake"


async def test_metrics_requires_admin(client: httpx.AsyncClient) -> None:
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"X-API-Key": VIEWER})).status_code == 403
    r = await client.get("/metrics", headers={"X-API-Key": ADMIN})
    assert r.status_code == 200 and "http_request_duration_seconds" in r.text


async def test_413_by_content_length() -> None:
    app = build_app(max_request_bytes=1024)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/complete", headers={"X-API-Key": APPROVER}, content=b"x" * 5000)
    assert_problem(r, 413, "payload-too-large")


async def test_413_when_streamed_body_exceeds_limit() -> None:
    app = build_app(max_request_bytes=1024)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"x" * 500

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/complete",
            headers={"X-API-Key": APPROVER, "Content-Type": "application/json"},
            content=chunks(),
        )
    assert_problem(r, 413, "payload-too-large")


async def test_logs_have_request_id_and_never_the_api_key() -> None:
    app = build_app()
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)  # create_app configured stdout; capture instead
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/v1/whoami", headers={"X-API-Key": ADMIN, "X-Request-ID": "trace-me-1"})
        await c.get("/boom", headers={"X-API-Key": ADMIN, "X-Request-ID": "trace-me-2"})

    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    assert ADMIN not in buf.getvalue()
    access = [e for e in lines if e.get("event") == "request"]
    assert {e["request_id"] for e in access} >= {"trace-me-1", "trace-me-2"}
    whoami = next(e for e in access if e["request_id"] == "trace-me-1")
    assert whoami["status"] == 200 and whoami["route"] == "/v1/whoami"
    assert whoami["key_id"] == "key_admin"  # who, without the secret
    errors = [e for e in lines if e.get("event") == "unhandled_error"]
    assert errors and errors[0]["request_id"] == "trace-me-2"
