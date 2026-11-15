"""ToolGate settings. Downstream credentials are ToolGate's own service identities."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr

from app.config import Settings


class GateSettings(Settings):
    app_name: str = "toolgate"
    worker_mode: str = "off"  # type: ignore[assignment]

    policy_path: str = "config/policies.yaml"
    downstream_timeout_s: float = Field(10.0, gt=0, le=60)
    max_args_bytes: int = Field(4096, ge=256)
    max_output_chars: int = Field(8000, ge=500)
    approval_ttl_s: int = Field(900, ge=30)
    kill_switch_cache_s: float = Field(1.0, ge=0, le=5)
    # A disabled or rotated token stops working within this many seconds (0 = look up every call).
    identity_cache_s: float = Field(1.0, ge=0, le=5)

    p1_url: str = "http://127.0.0.1:8002"
    p1_api_key: SecretStr | None = None
    p2_url: str = "http://127.0.0.1:8004"
    p2_token: SecretStr | None = None
    p3_shop_url: str = "http://127.0.0.1:8006"
    p3_shop_token: SecretStr = SecretStr("dev-shopsim-token")
    p4_url: str = "http://127.0.0.1:8007"
    p4_api_key: SecretStr | None = None

    # stdio transport (desktop clients): the client identity comes from this token.
    stdio_client_token: SecretStr | None = None


@lru_cache
def get_gate_settings() -> GateSettings:
    return GateSettings()
