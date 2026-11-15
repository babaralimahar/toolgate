"""ToolGate operations.

uv run python -m toolgate.cli create-client analyst-desktop     # prints the bearer token once
uv run python -m toolgate.cli rotate-token analyst-desktop
uv run python -m toolgate.cli disable-client finance-bot
uv run python -m toolgate.cli verify-audit                      # exit 1 if the chain is broken
uv run python -m toolgate.cli check-policy                      # validate config/policies.yaml
uv run python -m toolgate.cli stdio                             # MCP over stdio (desktop clients)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.adapters.db.engine import create_engine, create_sessionmaker
from toolgate import repo
from toolgate.config import get_gate_settings
from toolgate.policy import PolicyError, load_policy
from toolgate.tools import REGISTRY


async def _with_session(fn: str, name: str | None) -> int:
    s = get_gate_settings()
    engine = create_engine(s)
    sm = create_sessionmaker(engine)
    try:
        async with sm() as ss:
            if fn == "create":
                policy = load_policy(s.policy_path, set(REGISTRY))
                if name not in policy.clients:
                    print(f"client {name!r} is not defined in {s.policy_path}", file=sys.stderr)
                    return 2
                print(await repo.create_client(ss, str(name)))
                print("Store this token now; only its hash is kept.", file=sys.stderr)
            elif fn == "rotate":
                tok = await repo.rotate_token(ss, str(name))
                if tok is None:
                    print("no such client", file=sys.stderr)
                    return 2
                print(tok)
            elif fn == "disable":
                await repo.set_client_disabled(ss, str(name), True)
                print(f"disabled {name}")
            elif fn == "verify":
                r = await repo.verify_audit(ss)
                print(
                    f"audit chain {'OK' if r.ok else 'BROKEN'}: {r.checked} rows checked"
                    + ("" if r.ok else f"; first broken seq {r.first_broken_seq}: {r.problem}")
                )
                return 0 if r.ok else 1
    finally:
        await engine.dispose()
    return 0


def _stdio() -> int:
    from toolgate.gateway import Gateway
    from toolgate.mcp_server import build_mcp
    from toolgate.tools import Downstreams

    s = get_gate_settings()
    if s.stdio_client_token is None:
        print(
            "set STDIO_CLIENT_TOKEN to a client token (python -m toolgate.cli create-client <name>)",
            file=sys.stderr,
        )
        return 2
    policy = load_policy(s.policy_path, set(REGISTRY))
    gw = Gateway(policy, create_sessionmaker(create_engine(s)), Downstreams(s), s)
    build_mcp(gw, default_token=s.stdio_client_token.get_secret_value(), http_auth=False).run(
        "stdio"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolgate")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("create-client", "rotate-token", "disable-client"):
        sub.add_parser(cmd).add_argument("name")
    sub.add_parser("verify-audit")
    sub.add_parser("check-policy")
    sub.add_parser("stdio")
    a = p.parse_args(argv)
    if a.cmd == "check-policy":
        try:
            pol = load_policy(get_gate_settings().policy_path, set(REGISTRY))
        except PolicyError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"policy OK: {len(pol.clients)} clients, {len(pol.rules)} rules")
        return 0
    if a.cmd == "stdio":
        return _stdio()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    fn = {
        "create-client": "create",
        "rotate-token": "rotate",
        "disable-client": "disable",
        "verify-audit": "verify",
    }[a.cmd]
    return asyncio.run(_with_session(fn, getattr(a, "name", None)))


if __name__ == "__main__":
    raise SystemExit(main())
