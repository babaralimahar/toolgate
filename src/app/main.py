"""App factory. Run with: uvicorn app.main:create_app --factory

Projects pass their own routers and job handlers; the defaults are the template demo.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import __version__
from app.adapters.db.engine import create_engine, create_sessionmaker
from app.adapters.llm import LLMClient, load_pricing
from app.adapters.llm_factory import build_llm_client
from app.api import health, v1
from app.api.errors import install_error_handlers
from app.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.config import Settings, get_settings
from app.observability.logging import configure_logging
from app.observability.tracing import setup_tracing, shutdown_tracing
from app.services.handlers import HANDLERS
from app.worker import Handler, Worker

log = structlog.get_logger()


@dataclass(frozen=True)
class AppContext:
    """What job handlers need, built once per process."""

    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    llm: LLMClient


HandlerFactory = Callable[[AppContext], Mapping[str, Handler]]


def _template_handlers(_: AppContext) -> Mapping[str, Handler]:
    return HANDLERS


def create_app(
    settings: Settings | None = None,
    *,
    routers: Sequence[APIRouter] | None = None,
    handlers: HandlerFactory = _template_handlers,
    title: str | None = None,
    version: str = __version__,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings)  # lazy: no connection until the first query
    sessionmaker = create_sessionmaker(engine)
    llm = build_llm_client(settings, load_pricing(settings.pricing_path))
    ctx = AppContext(settings=settings, sessionmaker=sessionmaker, llm=llm)
    job_handlers = handlers(ctx)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        if settings.worker_mode == "embedded":
            worker = Worker(
                sessionmaker,
                job_handlers,
                poll_s=settings.worker_poll_s,
                lock_timeout_s=settings.job_lock_timeout_s,
            )
            worker_task = asyncio.create_task(worker.run_forever(stop), name="embedded-worker")
        log.info("startup", env=settings.env, version=version, worker=settings.worker_mode)
        try:
            yield
        finally:
            stop.set()
            if worker_task is not None:
                try:
                    await asyncio.wait_for(worker_task, timeout=10)
                except TimeoutError:
                    worker_task.cancel()
            aclose = getattr(llm, "aclose", None)
            if aclose is not None:
                await aclose()
            await engine.dispose()
            shutdown_tracing()
            log.info("shutdown")

    app = FastAPI(title=title or settings.app_name, version=version, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.llm = llm
    app.state.ctx = ctx

    install_error_handlers(app)
    app.include_router(health.router)
    for router in routers if routers is not None else [v1.router]:
        app.include_router(router)

    # add_middleware wraps the existing stack, so the last one added runs first.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(RequestContextMiddleware)

    setup_tracing(app, settings, engine)
    return app
