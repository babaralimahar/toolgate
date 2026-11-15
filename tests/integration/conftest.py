"""Integration tests run against a real Postgres (pgvector image).

Local:  docker compose up -d db      (TEST_DATABASE_URL defaults to 127.0.0.1:5436/app_test)
CI:     REQUIRE_DB=1 turns "database unreachable" into a failure instead of a skip.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parents[2]
REQUIRE_DB = os.environ.get("REQUIRE_DB") == "1"
TABLES = ("jobs", "items", "api_keys", "gate_clients", "gate_approvals", "gate_kill_switches")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


async def recreate_database(url: str) -> None:
    """Drop and create the database named in ``url`` (connecting to the 'postgres' db)."""
    target = make_url(url)
    name = target.database
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"refusing to recreate database {name!r}")
    admin = create_async_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """A fresh test database, migrated to head once per test session."""
    try:
        asyncio.run(recreate_database(TEST_DATABASE_URL))
    except (OSError, ConnectionError) as e:  # nothing listening
        if REQUIRE_DB:
            raise
        pytest.skip(
            f"Postgres not reachable at {TEST_DATABASE_URL} ({e}); run: docker compose up -d db"
        )
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, pool_size=10)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sm(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
