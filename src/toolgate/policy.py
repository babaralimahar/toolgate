"""Deny-by-default policy engine.

`decide(policy, client, tool, args, ctx)` is a pure function: same inputs, same Decision, no I/O.
That makes every authorization decision table-testable and explainable in the audit log.

Precedence: kill switch > unknown tool > disabled client > deny rule > missing scope >
argument constraint > require_approval > allow > default (deny).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Effect = Literal["allow", "deny", "require_approval"]
RATE = re.compile(r"^(\d+)/(s|sec|min|hour)$")


class PolicyError(Exception):
    """Invalid policy file: the gateway refuses to start (fail closed)."""


class Constraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_length: int | None = Field(None, ge=1)
    min: float | None = None
    max: float | None = None
    enum: list[Any] | None = None
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    effect: Effect
    scopes_required: list[str] = Field(default_factory=list)
    clients: list[str] | None = None  # optional: rule applies only to these clients
    constraints: dict[str, Constraint] = Field(default_factory=dict)
    rate_limit: str | None = None

    @field_validator("rate_limit")
    @classmethod
    def _rate(cls, v: str | None) -> str | None:
        if v is not None and not RATE.match(v):
            raise ValueError(f"rate_limit must look like '30/min', got {v!r}")
        return v


class ClientSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clients: dict[str, ClientSpec]
    rules: list[Rule]
    default: Literal["deny"] = "deny"  # allow-by-default is not representable on purpose


@dataclass(frozen=True)
class Client:
    name: str
    scopes: frozenset[str]
    disabled: bool = False


@dataclass(frozen=True)
class DecisionContext:
    global_kill: bool = False
    killed_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str
    client: str
    tool: str
    rule: Rule | None = None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "reason": self.reason,
            "client": self.client,
            "tool": self.tool,
            "rule": self.rule.tool if self.rule else None,
            "detail": self.detail,
        }


def load_policy(path: Path | str, known_tools: set[str]) -> Policy:
    """Parse and validate. Any problem raises PolicyError, and the server refuses to start."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        policy = Policy.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise PolicyError(f"invalid policy file {path}: {e}") from e
    unknown = {r.tool for r in policy.rules} - known_tools
    if unknown:
        raise PolicyError(f"policy references unknown tools: {sorted(unknown)}")
    for r in policy.rules:
        for c in r.clients or []:
            if c not in policy.clients:
                raise PolicyError(f"rule for {r.tool} names unknown client {c!r}")
    return policy


def _check(name: str, value: Any, c: Constraint) -> str | None:
    if c.max_length is not None and isinstance(value, str) and len(value) > c.max_length:
        return f"{name} longer than {c.max_length}"
    if c.enum is not None and value not in c.enum:
        return f"{name} must be one of {c.enum}"
    if c.pattern is not None and (not isinstance(value, str) or not re.fullmatch(c.pattern, value)):
        return f"{name} does not match the allowed pattern"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if c.min is not None and value < c.min:
            return f"{name} below {c.min}"
        if c.max is not None and value > c.max:
            return f"{name} above {c.max}"
    return None


def decide(
    policy: Policy,
    client: Client,
    tool: str,
    args: dict[str, Any],
    ctx: DecisionContext,
    known_tools: set[str],
) -> Decision:
    def d(effect: Effect, reason: str, rule: Rule | None = None, detail: str = "") -> Decision:
        return Decision(effect, reason, client.name, tool, rule, detail)

    if ctx.global_kill:
        return d("deny", "KILL_SWITCH", detail="all tools disabled")
    if tool in ctx.killed_tools:
        return d("deny", "KILL_SWITCH", detail=f"{tool} disabled")
    if tool not in known_tools:
        return d("deny", "UNKNOWN_TOOL")
    if client.disabled:
        return d("deny", "CLIENT_DISABLED")

    rules = [
        r
        for r in policy.rules
        if r.tool == tool and (r.clients is None or client.name in r.clients)
    ]
    denies = [r for r in rules if r.effect == "deny" and set(r.scopes_required) <= client.scopes]
    if denies:
        return d("deny", "DENY_RULE", denies[0])
    granting = [r for r in rules if r.effect != "deny"]
    if not granting:
        return d("deny", "NO_RULE")
    eligible = [r for r in granting if set(r.scopes_required) <= client.scopes]
    if not eligible:
        need = sorted(set(granting[0].scopes_required) - client.scopes)
        return d("deny", "MISSING_SCOPE", detail=f"needs {need}")
    for r in eligible:  # constraints of every eligible rule must hold (most restrictive wins)
        for name, c in r.constraints.items():
            if name in args and (err := _check(name, args[name], c)):
                return d("deny", "ARG_CONSTRAINT", r, err)
    approval = [r for r in eligible if r.effect == "require_approval"]
    if approval:
        return d("require_approval", "APPROVAL_REQUIRED", approval[0])
    return d("allow", "ALLOWED", eligible[0])


def parse_rate(spec: str) -> tuple[int, float]:
    m = RATE.match(spec)
    if not m:
        raise PolicyError(f"bad rate {spec!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n, {"s": 1.0, "sec": 1.0, "min": 60.0, "hour": 3600.0}[unit]
