"""End-to-end MCP check over streamable HTTP with a bearer token.

uv run python scripts/mcp_e2e.py <client-token> [url]
"""

from __future__ import annotations

import asyncio
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(token: str, url: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        streamable_http_client(url, http_client=http) as (read, write, *_),
        ClientSession(read, write) as s,
    ):
        await s.initialize()
        tools = await s.list_tools()
        print("tools:", [t.name for t in tools.tools])
        calls = [
            ("lookup_order", {"order_id": "10021"}),
            ("query_analytics", {"question": "Revenue by month?"}),
            ("lookup_order", {"order_id": "99999"}),
        ]
        for name, args in calls:
            r = await s.call_tool(name, args)
            body = getattr(r.content[0], "text", "") if r.content else ""
            print(f"{name}{args} -> is_error={r.is_error}: {body[:260]}")


if __name__ == "__main__":
    asyncio.run(
        main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8009/mcp/")
    )
