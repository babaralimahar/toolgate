from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_cosine_nearest_neighbour(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO items (text, embedding) VALUES "
                "('east', '[1,0,0]'), ('north', '[0,1,0]'), ('up', '[0,0,1]'), "
                "('north-east-ish', '[0.7,0.7,0]')"
            )
        )
        await s.commit()
        rows = (
            await s.execute(
                text(
                    "SELECT text, embedding <=> CAST(:q AS vector) AS dist FROM items "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 2"
                ),
                {"q": "[0.1,0.9,0]"},
            )
        ).all()
    assert [r.text for r in rows] == ["north", "north-east-ish"]
    assert rows[0].dist < rows[1].dist
