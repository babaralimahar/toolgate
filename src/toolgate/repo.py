"""Persistence: client identities, bound approvals, hash-chained audit, kill switches."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GENESIS = "0" * 64
AUDIT_LOCK = 0x70_6741_7564  # advisory lock key: one writer extends the chain at a time


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def args_hash(tool: str, args: dict[str, Any]) -> str:
    return sha(
        canonical({"tool": tool, "args": {k: v for k, v in args.items() if k != "approval_token"}})
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# ---------------------------------------------------------------- clients


async def create_client(ss: AsyncSession, name: str) -> str:
    token = "tg_" + secrets.token_urlsafe(32)
    await ss.execute(
        text("INSERT INTO gate_clients (id, name, token_hash) VALUES (:i, :n, :h)"),
        {"i": new_id("cl"), "n": name, "h": sha(token)},
    )
    await ss.commit()
    return token


async def rotate_token(ss: AsyncSession, name: str) -> str | None:
    token = "tg_" + secrets.token_urlsafe(32)
    r = await ss.execute(
        text(
            "UPDATE gate_clients SET token_hash = :h, rotated_at = now() WHERE name = :n RETURNING id"
        ),
        {"h": sha(token), "n": name},
    )
    await ss.commit()
    return token if r.first() else None


async def client_by_token(ss: AsyncSession, token: str) -> tuple[str, bool] | None:
    r = (
        await ss.execute(
            text("SELECT name, disabled FROM gate_clients WHERE token_hash = :h"), {"h": sha(token)}
        )
    ).first()
    return (r.name, bool(r.disabled)) if r else None


async def set_client_disabled(ss: AsyncSession, name: str, disabled: bool) -> None:
    await ss.execute(
        text("UPDATE gate_clients SET disabled = :d WHERE name = :n"), {"d": disabled, "n": name}
    )
    await ss.commit()


# ---------------------------------------------------------------- approvals


async def create_approval(
    ss: AsyncSession, client: str, tool: str, ahash: str, preview: dict[str, Any], ttl_s: int
) -> str:
    aid = new_id("apr")
    await ss.execute(
        text(
            "INSERT INTO gate_approvals (id, client, tool, args_hash, args_preview, expires_at) "
            "VALUES (:i, :c, :t, :h, CAST(:p AS jsonb), :e)"
        ),
        {
            "i": aid,
            "c": client,
            "t": tool,
            "h": ahash,
            "p": canonical(preview),
            "e": datetime.now(UTC) + timedelta(seconds=ttl_s),
        },
    )
    await ss.commit()
    return aid


async def decide_approval(ss: AsyncSession, approval_id: str, approve: bool, by: str) -> str | None:
    """Pending -> approved (returns a one-time token, shown once) or denied. None if not pending."""
    token = "apt_" + secrets.token_urlsafe(24) if approve else None
    r = await ss.execute(
        text(
            "UPDATE gate_approvals SET status = :s, decided_by = :b, token_hash = :h "
            "WHERE id = :i AND status = 'pending' AND expires_at > now() RETURNING id"
        ),
        {
            "s": "approved" if approve else "denied",
            "b": by,
            "h": sha(token) if token else None,
            "i": approval_id,
        },
    )
    await ss.commit()
    if r.first() is None:
        return None
    return token or ""


async def consume_approval(
    ss: AsyncSession, token: str, client: str, tool: str, ahash: str
) -> bool:
    """Atomically use an approval token: only once, only for the same client + tool + args hash,
    only before expiry."""
    r = await ss.execute(
        text(
            "UPDATE gate_approvals SET status = 'used', used_at = now() WHERE token_hash = :h AND status = 'approved' "
            "AND client = :c AND tool = :t AND args_hash = :a AND used_at IS NULL AND expires_at > now() RETURNING id"
        ),
        {"h": sha(token), "c": client, "t": tool, "a": ahash},
    )
    await ss.commit()
    return r.first() is not None


async def list_approvals(ss: AsyncSession, status: str = "pending") -> list[dict[str, Any]]:
    rows = (
        (
            await ss.execute(
                text(
                    "SELECT id, client, tool, args_preview, status, created_at, expires_at FROM gate_approvals "
                    "WHERE status = :s ORDER BY created_at"
                ),
                {"s": status},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- audit (hash chain)

AUDIT_FIELDS = (
    "seq",
    "ts",
    "client",
    "tool",
    "args_hash",
    "decision",
    "reason",
    "status",
    "latency_ms",
    "trace_id",
    "cost_usd",
)


@dataclass
class AuditEntry:
    client: str
    tool: str
    args_hash: str
    decision: str
    reason: str
    status: str
    latency_ms: int
    trace_id: str
    cost_usd: float = 0.0


def row_hash(prev_hash: str, row: dict[str, Any]) -> str:
    body = {k: row[k] for k in AUDIT_FIELDS}
    body["ts"] = (
        row["ts"].astimezone(UTC).isoformat() if isinstance(row["ts"], datetime) else str(row["ts"])
    )
    body["cost_usd"] = format(Decimal(str(row["cost_usd"])).quantize(Decimal("0.000001")), "f")
    return sha(prev_hash + canonical(body))


async def append_audit(ss: AsyncSession, e: AuditEntry) -> int:
    """Append one row, extending the chain. Serialised by a transaction-scoped advisory lock so
    two writers can never fork the chain. If this raises, the call fails: no unaudited actions."""
    await ss.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK})
    last = (
        await ss.execute(text("SELECT seq, hash FROM gate_audit ORDER BY seq DESC LIMIT 1"))
    ).first()
    seq, prev = (last.seq + 1, last.hash) if last else (1, GENESIS)
    row = {
        "seq": seq,
        "ts": datetime.now(UTC).replace(microsecond=0),
        "client": e.client,
        "tool": e.tool,
        "args_hash": e.args_hash,
        "decision": e.decision,
        "reason": e.reason,
        "status": e.status,
        "latency_ms": e.latency_ms,
        "trace_id": e.trace_id,
        "cost_usd": e.cost_usd,
    }
    h = row_hash(prev, row)
    await ss.execute(
        text(
            "INSERT INTO gate_audit (seq, ts, client, tool, args_hash, decision, reason, status, latency_ms, trace_id, "
            "cost_usd, prev_hash, hash) VALUES (:seq, :ts, :client, :tool, :args_hash, :decision, :reason, :status, "
            ":latency_ms, :trace_id, :cost_usd, :prev, :hash)"
        ),
        row | {"prev": prev, "hash": h},
    )
    await ss.commit()
    return seq


@dataclass
class VerifyResult:
    ok: bool
    checked: int
    first_broken_seq: int | None = None
    problem: str = ""


async def verify_audit(ss: AsyncSession) -> VerifyResult:
    """Recompute the chain from the genesis. Reports the first row whose content or link does
    not match - an edited row, a deleted row (the next row's prev_hash dangles) or a reordering."""
    prev, expected_seq, n = GENESIS, 1, 0
    result = await ss.stream(
        text(f"SELECT {', '.join(AUDIT_FIELDS)}, prev_hash, hash FROM gate_audit ORDER BY seq")  # noqa: S608  (constant column list)
    )
    async for r in result.mappings():
        n += 1
        if r["seq"] != expected_seq:
            return VerifyResult(False, n, r["seq"], f"sequence gap: expected {expected_seq}")
        if r["prev_hash"] != prev:
            return VerifyResult(False, n, r["seq"], "prev_hash does not match the previous row")
        if row_hash(prev, dict(r)) != r["hash"]:
            return VerifyResult(False, n, r["seq"], "row content does not match its hash")
        prev, expected_seq = r["hash"], expected_seq + 1
    return VerifyResult(True, n)


# ---------------------------------------------------------------- kill switches


async def set_kill_switch(ss: AsyncSession, scope: str, enabled: bool, by: str) -> None:
    await ss.execute(
        text(
            "INSERT INTO gate_kill_switches (scope, enabled, updated_by) VALUES (:s, :e, :b) "
            "ON CONFLICT (scope) DO UPDATE SET enabled = EXCLUDED.enabled, updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"s": scope, "e": enabled, "b": by},
    )
    await ss.commit()


async def kill_switches(ss: AsyncSession) -> tuple[bool, frozenset[str]]:
    rows = (
        (await ss.execute(text("SELECT scope FROM gate_kill_switches WHERE enabled")))
        .scalars()
        .all()
    )
    return "global" in rows, frozenset(
        s.removeprefix("tool:") for s in rows if s.startswith("tool:")
    )
