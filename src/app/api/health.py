"""Liveness (/healthz), readiness (/readyz) and Prometheus metrics (/metrics)."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.adapters.db.engine import ping
from app.api.auth import Principal, require_role
from app.api.errors import DomainError

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """The process is up. Never touches dependencies (a DB outage must not restart pods)."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, Any]:
    """Ready to serve traffic: the database answers within 2 s."""
    try:
        await ping(request.app.state.engine, timeout_s=2.0)
    except Exception as e:
        structlog.get_logger().warning("readiness_failed", error=type(e).__name__)
        raise DomainError(503, "unavailable", "Not ready", "database unreachable") from e
    return {"status": "ready", "checks": {"database": "ok"}}


@router.get("/metrics", include_in_schema=False)
async def metrics(_: Annotated[Principal, Depends(require_role("admin"))]) -> Response:
    """Admin-only, because the service is on the public internet."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
