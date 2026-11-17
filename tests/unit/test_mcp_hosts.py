"""The MCP endpoint keeps DNS-rebinding protection on and only answers to configured hosts."""

from __future__ import annotations

from toolgate.config import GateSettings


def _s(hosts: str) -> GateSettings:
    return GateSettings(database_url="postgresql+asyncpg://u:p@h/db", mcp_public_hosts=hosts)


def test_default_is_sdk_localhost_only() -> None:
    assert _s("").mcp_transport_security() is None


def test_public_host_allowed_and_protection_stays_on() -> None:
    ts = _s("toolgate-a2or.onrender.com, other.example").mcp_transport_security()
    assert ts is not None and ts.enable_dns_rebinding_protection
    assert "toolgate-a2or.onrender.com" in ts.allowed_hosts
    assert "other.example" in ts.allowed_hosts
    assert "https://toolgate-a2or.onrender.com" in ts.allowed_origins
    assert "evil.example" not in ts.allowed_hosts
