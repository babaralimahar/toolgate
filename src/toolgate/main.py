"""ToolGate app: admin API + MCP (streamable HTTP) at /mcp/.

uv run uvicorn toolgate.main:create_app --factory --port 8009
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.main import AppContext
from app.main import create_app as create_base_app
from app.worker import Handler
from toolgate import __version__
from toolgate.admin import router
from toolgate.config import GateSettings, get_gate_settings
from toolgate.gateway import Gateway
from toolgate.mcp_server import build_mcp
from toolgate.policy import load_policy
from toolgate.tools import REGISTRY, Downstreams


def _no_handlers(_: AppContext) -> Mapping[str, Handler]:
    return {}


def create_app(
    settings: GateSettings | None = None, downstreams: Downstreams | None = None
) -> FastAPI:
    s = settings or get_gate_settings()
    policy = load_policy(
        s.policy_path, set(REGISTRY)
    )  # invalid policy -> PolicyError: refuse to start
    app = create_base_app(
        s, routers=[router], handlers=_no_handlers, title="ToolGate", version=__version__
    )
    ds = downstreams or Downstreams(s)
    gw = Gateway(policy, app.state.sessionmaker, ds, s)
    mcp = build_mcp(gw)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=s.mcp_transport_security(),
    )
    app.mount("/mcp", mcp_app)
    app.state.gateway = gw
    app.state.mcp = mcp

    base_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[None]:
        async with base_lifespan(a), mcp.session_manager.run():
            yield
        await ds.aclose()

    app.router.lifespan_context = lifespan

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/dashboard")

    return app
