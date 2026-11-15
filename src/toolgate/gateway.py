"""The governed call path, independent of the MCP transport:

received -> identify -> validate -> decide (kill switch + policy) -> [approval] -> rate limit ->
forward (timeout, trace context) -> label + cap output -> audit (or fail the call)
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry import trace
from prometheus_client import Counter, Histogram
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from toolgate import repo
from toolgate.config import GateSettings
from toolgate.policy import Client, Decision, DecisionContext, Policy, decide, parse_rate
from toolgate.tools import REGISTRY, DownstreamError, Downstreams, ToolSpec, label_output

tracer = trace.get_tracer("toolgate")
CALLS = Counter("toolgate_calls_total", "Tool calls", ["tool", "decision", "status"])
LATENCY = Histogram(
    "toolgate_call_seconds",
    "Gateway call latency incl. downstream",
    ["tool"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
COST = Counter(
    "toolgate_downstream_cost_usd_total", "LLM cost reported by downstream services", ["tool"]
)

Status = Literal["ok", "denied", "pending_approval", "rejected", "tool_error"]


class AuditUnavailable(Exception):
    """The audit write failed, so the call is failed too: no unaudited actions."""


@dataclass
class CallResult:
    status: Status
    tool: str
    client: str
    reason: str = ""
    detail: str = ""
    output: dict[str, Any] | None = None
    approval_id: str | None = None
    audit_seq: int | None = None
    trace_id: str = ""
    latency_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "tool": self.tool, "reason": self.reason}
        if self.detail:
            d["detail"] = self.detail
        if self.approval_id:
            d["approval_id"] = self.approval_id
            d["next_step"] = (
                "Ask an administrator to approve, then call again with the same arguments and approval_token."
            )
        if self.output is not None:
            d["result"] = self.output
        d["audit_seq"] = self.audit_seq
        return d


class RateLimiter:
    """Sliding window per (client, tool). In memory: one gateway process (Redis for several)."""

    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, client: str, tool: str, spec: str) -> bool:
        n, window = parse_rate(spec)
        now = time.monotonic()
        q = self.hits[(client, tool)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= n:
            return False
        q.append(now)
        return True


@dataclass
class KillSwitchCache:
    ttl_s: float
    value: tuple[bool, frozenset[str]] = (False, frozenset())
    at: float = field(default=-1e9)

    def invalidate(self) -> None:
        self.at = -1e9


class Gateway:
    def __init__(
        self,
        policy: Policy,
        sm: async_sessionmaker[AsyncSession],
        downstreams: Downstreams,
        settings: GateSettings,
        registry: dict[str, ToolSpec] | None = None,
    ) -> None:
        self.policy, self.sm, self.ds, self.s = policy, sm, downstreams, settings
        self.registry = registry or REGISTRY
        self.known = set(self.registry)
        self.rates = RateLimiter()
        self.kills = KillSwitchCache(settings.kill_switch_cache_s)
        self._ids: dict[str, tuple[float, Client | None]] = {}

    async def _kill_state(self) -> tuple[bool, frozenset[str]]:
        if time.monotonic() - self.kills.at > self.kills.ttl_s:
            async with self.sm() as ss:
                self.kills.value = await repo.kill_switches(ss)
            self.kills.at = time.monotonic()
        return self.kills.value

    async def _identify(self, token: str | None) -> Client | None:
        if not token:
            return None
        key = repo.sha(token)  # never keep raw tokens in memory longer than the call
        hit = self._ids.get(key)
        if hit and time.monotonic() - hit[0] < self.s.identity_cache_s:
            return hit[1]
        async with self.sm() as ss:
            found = await repo.client_by_token(ss, token)
        client = None
        if found is not None:
            name, disabled = found
            spec = self.policy.clients.get(name)
            scopes = (
                frozenset(spec.scopes) if spec else frozenset()
            )  # identity not in policy -> no scopes
            client = Client(name, scopes, disabled)
        if len(self._ids) > 10_000:
            self._ids.clear()
        self._ids[key] = (time.monotonic(), client)
        return client

    async def call(self, token: str | None, tool: str, args: dict[str, Any]) -> CallResult:
        t0 = time.perf_counter()
        with tracer.start_as_current_span(f"toolgate.call {tool}") as span:
            span.set_attribute("toolgate.tool", tool)
            trace_id = format(span.get_span_context().trace_id, "032x")
            res = await self._call(token, tool, dict(args), trace_id)
            res.trace_id = trace_id
            res.latency_ms = int((time.perf_counter() - t0) * 1000)
            span.set_attribute("toolgate.status", res.status)
            span.set_attribute("toolgate.reason", res.reason)
        LATENCY.labels(tool if tool in self.known else "_unknown").observe(res.latency_ms / 1000)
        return res

    async def _call(
        self, token: str | None, tool: str, args: dict[str, Any], trace_id: str
    ) -> CallResult:
        t0 = time.perf_counter()
        approval_token = args.pop("approval_token", None)
        ahash = repo.args_hash(tool, args)
        client = await self._identify(token)
        cname = client.name if client else "unauthenticated"

        async def finish(res: CallResult, decision: str, cost: float = 0.0) -> CallResult:
            entry = repo.AuditEntry(
                cname,
                tool[:80],
                ahash,
                decision,
                res.reason or "OK",
                res.status,
                int((time.perf_counter() - t0) * 1000),
                trace_id,
                cost,
            )
            try:
                async with self.sm() as ss:
                    res.audit_seq = await repo.append_audit(ss, entry)
            except Exception as e:  # any audit failure fails the call
                raise AuditUnavailable("audit log unavailable; call refused") from e
            CALLS.labels(tool if tool in self.known else "_unknown", decision, res.status).inc()
            return res

        if client is None:
            return await finish(CallResult("denied", tool, cname, "UNAUTHENTICATED"), "deny")
        if len(json.dumps(args, default=str)) > self.s.max_args_bytes:
            return await finish(CallResult("rejected", tool, cname, "ARGS_TOO_LARGE"), "deny")

        spec = self.registry.get(tool)
        model = None
        if spec is not None:
            try:
                model = spec.args.model_validate(args)
            except ValidationError as e:
                msg = "; ".join(
                    f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3]
                )
                return await finish(
                    CallResult("rejected", tool, cname, "INVALID_ARGS", msg), "deny"
                )

        killed_all, killed = await self._kill_state()
        d: Decision = decide(
            self.policy, client, tool, args, DecisionContext(killed_all, killed), self.known
        )
        if d.effect == "deny":
            return await finish(CallResult("denied", tool, cname, d.reason, d.detail), "deny")
        assert spec is not None and model is not None  # noqa: S101  (decide denies unknown tools)

        if d.effect == "require_approval":
            if not approval_token:
                async with self.sm() as ss:
                    aid = await repo.create_approval(
                        ss, cname, tool, ahash, args, self.s.approval_ttl_s
                    )
                return await finish(
                    CallResult(
                        "pending_approval", tool, cname, "APPROVAL_REQUIRED", approval_id=aid
                    ),
                    "require_approval",
                )
            async with self.sm() as ss:
                if not await repo.consume_approval(ss, str(approval_token), cname, tool, ahash):
                    return await finish(
                        CallResult(
                            "denied",
                            tool,
                            cname,
                            "APPROVAL_INVALID",
                            "token unknown, already used, expired, or for different arguments",
                        ),
                        "deny",
                    )

        if d.rule and d.rule.rate_limit and not self.rates.allow(cname, tool, d.rule.rate_limit):
            return await finish(
                CallResult("denied", tool, cname, "RATE_LIMITED", d.rule.rate_limit), "deny"
            )

        decision = "allow" if d.effect == "allow" else "approved"
        try:
            with tracer.start_as_current_span(f"downstream {tool}"):
                out = await spec.call(self.ds, model)
        except DownstreamError as e:
            # Structured tool error, never a hang; no retry (a write tool might not be idempotent).
            return await finish(
                CallResult("tool_error", tool, cname, "DOWNSTREAM_ERROR", str(e)), decision
            )
        COST.labels(tool).inc(out.cost_usd)
        labelled = label_output(tool, out.data, self.s.max_output_chars)
        return await finish(
            CallResult("ok", tool, cname, "OK", output=labelled), decision, out.cost_usd
        )
