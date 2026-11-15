"""Operator commands.

uv run python -m app.cli create-key --name me --role admin
uv run python -m app.cli disable-key key_abc123
uv run python -m app.cli llm-smoke "Say hello in five words"
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.adapters.db.api_keys import SqlApiKeyRepo
from app.adapters.db.engine import create_engine, create_sessionmaker
from app.adapters.llm import complete_with_retries, load_pricing
from app.adapters.llm_factory import build_llm_client
from app.config import get_settings
from app.domain.roles import ROLES, Role


async def _create_key(name: str, role: Role) -> None:
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as s:
            key_id, raw = await SqlApiKeyRepo(s).create(name, role)
    finally:
        await engine.dispose()
    print(f"key_id: {key_id}\nrole:   {role}\napi_key: {raw}")
    print("Store the api_key now (password manager). It is not shown again.", file=sys.stderr)


async def _disable_key(key_id: str) -> int:
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine)() as s:
            ok = await SqlApiKeyRepo(s).disable(key_id)
    finally:
        await engine.dispose()
    print("disabled" if ok else "not found")
    return 0 if ok else 1


async def _llm_smoke(prompt: str) -> None:
    settings = get_settings()
    llm = build_llm_client(settings, load_pricing(settings.pricing_path))
    res = await complete_with_retries(
        llm,
        settings.llm_max_attempts,
        prompt_version="smoke-v1",
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"model:      {res.model}")
    print(f"text:       {res.text!r}")
    print(f"tokens:     in={res.usage.tokens_in} out={res.usage.tokens_out}")
    print(f"cost (USD): {res.usage.cost_usd}  (from {settings.pricing_path})")
    print(f"latency:    {res.latency_ms} ms")
    aclose = getattr(llm, "aclose", None)
    if aclose is not None:
        await aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ck = sub.add_parser("create-key", help="create an API key (printed once)")
    ck.add_argument("--name", required=True)
    ck.add_argument("--role", required=True, choices=ROLES)
    dk = sub.add_parser("disable-key", help="disable an API key by id")
    dk.add_argument("key_id")
    sm = sub.add_parser("llm-smoke", help="one real call to the configured provider")
    sm.add_argument("prompt")
    args = parser.parse_args(argv)

    if args.cmd == "create-key":
        asyncio.run(_create_key(args.name, args.role))
        return 0
    if args.cmd == "disable-key":
        return asyncio.run(_disable_key(args.key_id))
    asyncio.run(_llm_smoke(args.prompt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
