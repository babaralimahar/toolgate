"""Pure-ASGI middleware: request ids + access log + latency metric, and a body size limit."""

from __future__ import annotations

import json
import re
import time
import uuid

import structlog
from fastapi import HTTPException
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.context import request_id_var
from app.api.errors import PROBLEM_JSON, problem_body
from app.observability.metrics import HTTP_LATENCY

# Accept a caller's id only if it is short and plain, so it cannot inject into logs.
_VALID_RID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
log = structlog.get_logger("http")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


class RequestContextMiddleware:
    """Assigns X-Request-ID, binds it to logs, logs one line per request, records latency."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        rid = (
            incoming if incoming and _VALID_RID.match(incoming) else f"req_{uuid.uuid4().hex[:16]}"
        )
        # Not reset afterwards on purpose: the 500 handler runs after this middleware
        # returns and still needs the id. Each request runs in its own context.
        request_id_var.set(rid)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)

        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            route_label = getattr(route, "path", "unmatched")  # template path, not raw URL
            HTTP_LATENCY.labels(scope["method"], route_label, str(status)).observe(elapsed)
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                route=route_label,
                status=status,
                latency_ms=round(elapsed * 1000, 1),
            )


class BodySizeLimitMiddleware:
    """Rejects bodies over ``max_bytes`` with 413, by Content-Length or while streaming."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._reject(scope, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI's HTTPException (not Starlette's base class): FastAPI re-raises
                    # it from body parsing instead of turning it into a generic 400.
                    raise HTTPException(413, f"body exceeds {self.max_bytes} bytes")
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            problem_body(
                413,
                "payload-too-large",
                "Payload too large",
                f"body exceeds {self.max_bytes} bytes",
                scope["path"],
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", PROBLEM_JSON.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
