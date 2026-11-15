"""RFC 9457 problem+json errors. Clients never see stack traces or internals."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.context import request_id_var

PROBLEM_JSON = "application/problem+json"
ERROR_TYPE_BASE = "https://errors.example.dev/"  # replace with your docs URL per project

_HTTP_CODES = {
    400: "bad-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
    413: "payload-too-large",
    415: "unsupported-media-type",
    429: "rate-limited",
    503: "unavailable",
}


class DomainError(Exception):
    def __init__(self, status: int, code: str, title: str, detail: str = "") -> None:
        super().__init__(title)
        self.status, self.code, self.title, self.detail = status, code, title, detail


def problem_body(
    status: int, code: str, title: str, detail: str, instance: str, **extra: Any
) -> dict[str, Any]:
    return {
        "type": f"{ERROR_TYPE_BASE}{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id_var.get(),
        **extra,
    }


def problem(
    request: Request, status: int, code: str, title: str, detail: str = "", **extra: Any
) -> JSONResponse:
    rid = request_id_var.get()
    return JSONResponse(
        problem_body(status, code, title, detail, request.url.path, **extra),
        status_code=status,
        media_type=PROBLEM_JSON,
        # Set here too: a 500 is rendered outside the request-id middleware.
        headers={"X-Request-ID": rid},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(req: Request, exc: DomainError) -> JSONResponse:
        resp = problem(req, exc.status, exc.code, exc.title, exc.detail)
        if exc.status == 401:
            resp.headers["WWW-Authenticate"] = 'ApiKey header="X-API-Key"'
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation(req: Request, exc: RequestValidationError) -> JSONResponse:
        # Report where and why, never the submitted values (they may be sensitive).
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()[:10]
        ]
        return problem(
            req,
            422,
            "validation-error",
            "Invalid request",
            f"{len(exc.errors())} error(s)",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(req: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http-error")
        title = code.replace("-", " ").capitalize()
        detail = exc.detail if isinstance(exc.detail, str) else ""
        return problem(req, exc.status_code, code, title, detail)

    @app.exception_handler(Exception)
    async def _unexpected(req: Request, exc: Exception) -> JSONResponse:
        structlog.get_logger().exception("unhandled_error", path=req.url.path)  # detail: logs only
        return problem(req, 500, "internal", "Internal error")
