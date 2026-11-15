"""Gateway overhead benchmark (N1): the latency ToolGate adds, excluding the downstream.

The downstream is an in-process stub that answers instantly, so the measured time is the
governance path: token lookup, validation, kill-switch read (cached), decide(), rate limit,
output labelling and the advisory-locked, hash-chained audit insert.

    uv run python -m toolgate.bench --n 500
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.adapters.db.engine import create_engine, create_sessionmaker
from toolgate import repo
from toolgate.config import get_gate_settings
from toolgate.gateway import Gateway
from toolgate.policy import Client, DecisionContext, decide, load_policy
from toolgate.tools import REGISTRY, Downstreams


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


async def main(n: int) -> str:
    s = get_gate_settings().model_copy(update={"kill_switch_cache_s": 1.0})
    policy = load_policy(s.policy_path, set(REGISTRY))
    engine = create_engine(s)
    sm = create_sessionmaker(engine)

    def instant(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "10021", "status": "delivered", "total": "18.00", "items": []}
        )

    ds = Downstreams(s, transport=httpx.MockTransport(instant))
    gw = Gateway(policy, sm, ds, s)
    async with sm() as ss:
        token = await repo.rotate_token(ss, "support-agent") or await repo.create_client(
            ss, "support-agent"
        )
    for _ in range(20):  # warm-up: connections, caches
        await gw.call(token, "lookup_order", {"order_id": "10021"})
    gw.rates.hits.clear()
    rule = next(r for r in policy.rules if r.tool == "lookup_order")
    object.__setattr__(rule, "rate_limit", None)  # measure the path, not the limiter's refusals
    lat: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        res = await gw.call(token, "lookup_order", {"order_id": "10021"})
        lat.append((time.perf_counter() - t0) * 1000)
        assert res.status == "ok", res  # noqa: S101
    c = Client("support-agent", frozenset(policy.clients["support-agent"].scopes))
    t0 = time.perf_counter()
    for _ in range(10_000):
        decide(policy, c, "lookup_order", {"order_id": "10021"}, DecisionContext(), set(REGISTRY))
    decide_us = (time.perf_counter() - t0) / 10_000 * 1e6
    async with sm() as ss:
        v = await repo.verify_audit(ss)
    await ds.aclose()
    await engine.dispose()
    return (
        f"# ToolGate overhead (stubbed downstream)\n\n"
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC · {n} sequential allowed calls · local Postgres 17 (Docker) · "
        f"{sys.platform}\n\n| Measure | Value |\n|---|---|\n"
        f"| Gateway overhead p50 | {pct(lat, 0.5):.2f} ms |\n| Gateway overhead p95 | {pct(lat, 0.95):.2f} ms |\n"
        f"| Gateway overhead p99 | {pct(lat, 0.99):.2f} ms |\n| Mean | {statistics.mean(lat):.2f} ms |\n"
        f"| `decide()` alone | {decide_us:.1f} µs |\n| Audit chain after run | {'intact' if v.ok else 'BROKEN'} ({v.checked} rows) |\n\n"
        "Method: in-process `Gateway.call` with an httpx MockTransport downstream that answers instantly, so the "
        "numbers are the governance path only (token hash lookup, validation, cached kill-switch read, decide(), "
        "output labelling, advisory-locked hash-chained audit insert). Sequential calls; concurrent writers "
        "serialise on the audit lock, which bounds audited throughput per database.\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=500)
    a = p.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    md = asyncio.run(main(a.n))
    out = Path("evals/reports/overhead.md")
    out.write_text(md, encoding="utf-8")
    print(md)
