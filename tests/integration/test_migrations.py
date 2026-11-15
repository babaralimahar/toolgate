from __future__ import annotations

import asyncio

from alembic import command
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL
from tests.integration.conftest import alembic_config, recreate_database


async def _tables(url: str) -> set[str]:
    eng = create_async_engine(url)
    try:
        async with eng.connect() as conn:
            rows = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {r[0] for r in rows}
    finally:
        await eng.dispose()


def test_upgrade_downgrade_upgrade_on_empty_db(migrated_db: str) -> None:
    url = (
        make_url(TEST_DATABASE_URL)
        .set(database="app_migrations_test")
        .render_as_string(hide_password=False)
    )
    asyncio.run(recreate_database(url))
    cfg = alembic_config(url)

    p1_tables = {
        "gate_clients",
        "gate_approvals",
        "gate_audit",
        "gate_kill_switches",
    }  # migration 0002
    command.upgrade(cfg, "head")
    assert {"api_keys", "jobs", "items", "alembic_version"} | p1_tables <= asyncio.run(_tables(url))

    command.downgrade(cfg, "-1")  # 0002 off: project tables gone, template tables stay
    tables = asyncio.run(_tables(url))
    assert not tables & p1_tables and {"api_keys", "jobs"} <= tables

    command.downgrade(cfg, "base")
    assert asyncio.run(_tables(url)) == {"alembic_version"}

    command.upgrade(cfg, "head")
    assert {"api_keys", "jobs", "items"} | p1_tables <= asyncio.run(_tables(url))
