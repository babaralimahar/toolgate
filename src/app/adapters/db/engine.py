"""Async engine, session factory and a readiness ping."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Creating the engine does not connect; the first query does."""
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # survive hosts that drop idle connections (Neon scales to zero)
        pool_recycle=1800,
        connect_args={
            "command_timeout": 30,
            "server_settings": {"application_name": settings.app_name},
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine, timeout_s: float = 2.0) -> None:
    async with asyncio.timeout(timeout_s), engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
