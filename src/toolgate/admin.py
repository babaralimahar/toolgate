"""Admin API: approvals, kill switches, audit verification, dashboard. Admin role (template API keys)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import text

from app.api.auth import Principal, require_role
from app.api.deps import SessionDep
from app.api.errors import DomainError
from toolgate import repo
from toolgate.gateway import Gateway

router = APIRouter(prefix="/admin", tags=["admin"])
Admin = Annotated[Principal, Depends(require_role("admin"))]


def gateway(request: Request) -> Gateway:
    return cast(Gateway, request.app.state.gateway)


@router.get("/approvals")
async def approvals(session: SessionDep, _: Admin, status: str = "pending") -> dict[str, Any]:
    return {
        "items": [
            {k: repo.canonical(v) if isinstance(v, dict) else str(v) for k, v in a.items()}
            for a in await repo.list_approvals(session, status)
        ]
    }


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]


@router.post("/approvals/{approval_id}")
async def decide(
    approval_id: str, body: ApprovalDecision, session: SessionDep, p: Admin
) -> dict[str, Any]:
    token = await repo.decide_approval(session, approval_id, body.decision == "approve", p.key_id)
    if token is None:
        raise DomainError(409, "conflict", "Approval is not pending (or expired)")
    out: dict[str, Any] = {"approval_id": approval_id, "decision": body.decision}
    if token:
        out["approval_token"] = (
            token  # shown once; valid once, for the same client + tool + arguments
        )
    return out


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["global", "tool"]
    tool: str | None = None
    enabled: bool

    @model_validator(mode="after")
    def _tool_needed(self) -> KillSwitchIn:
        if (self.scope == "tool") != (self.tool is not None):
            raise ValueError("tool is required exactly when scope is 'tool'")
        return self


@router.post("/kill-switch")
async def kill_switch(
    body: KillSwitchIn, request: Request, session: SessionDep, p: Admin
) -> dict[str, Any]:
    gw = gateway(request)
    if body.tool is not None and body.tool not in gw.known:
        raise DomainError(404, "not-found", "Unknown tool")
    scope = "global" if body.scope == "global" else f"tool:{body.tool}"
    await repo.set_kill_switch(session, scope, body.enabled, p.key_id)
    gw.kills.invalidate()  # this process: effective on the very next call; others within kill_switch_cache_s
    return {"scope": scope, "enabled": body.enabled}


@router.get("/audit/verify")
async def verify(session: SessionDep, _: Admin) -> dict[str, Any]:
    r = await repo.verify_audit(session)
    return {
        "ok": r.ok,
        "rows_checked": r.checked,
        "first_broken_seq": r.first_broken_seq,
        "problem": r.problem,
    }


DASH_SQL = """
SELECT tool,
       count(*) AS calls,
       count(*) FILTER (WHERE decision = 'deny') AS denies,
       count(*) FILTER (WHERE status = 'tool_error') AS errors,
       count(*) FILTER (WHERE status = 'pending_approval') AS pending,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
       sum(cost_usd) AS cost_usd
FROM gate_audit WHERE ts > now() - make_interval(hours => :h)
GROUP BY tool ORDER BY calls DESC
"""


async def dashboard_data(session: Any, hours: int = 24) -> dict[str, Any]:
    rows = (await session.execute(text(DASH_SQL), {"h": hours})).mappings().all()
    per_min = (
        await session.execute(
            text("SELECT count(*) FROM gate_audit WHERE ts > now() - interval '10 minutes'")
        )
    ).scalar_one()
    reasons = (
        await session.execute(
            text(
                "SELECT reason, count(*) AS n FROM gate_audit WHERE decision = 'deny' AND ts > now() - make_interval(hours => :h) "
                "GROUP BY reason ORDER BY n DESC"
            ),
            {"h": hours},
        )
    ).all()
    return {
        "window_hours": hours,
        "calls_per_min_last_10m": round(per_min / 10, 2),
        "tools": [
            {
                k: (float(v) if k in ("p95_ms", "cost_usd") and v is not None else v)
                for k, v in r.items()
            }
            for r in rows
        ],
        "deny_reasons": {r.reason: r.n for r in reasons},
    }


@router.get("/dashboard.json")
async def dashboard_json(session: SessionDep, _: Admin, hours: int = 24) -> dict[str, Any]:
    return await dashboard_data(session, hours)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(session: SessionDep, _: Admin, hours: int = 24) -> str:
    d = await dashboard_data(session, hours)
    verify_res = await repo.verify_audit(session)
    rows = "".join(
        f"<tr><td>{t['tool']}</td><td>{t['calls']}</td><td>{t['denies']}</td><td>{t['errors']}</td>"
        f"<td>{t['pending']}</td><td>{(t['p95_ms'] or 0):.0f}</td><td>${(t['cost_usd'] or 0):.5f}</td></tr>"
        for t in d["tools"]
    )
    reasons = ", ".join(f"{k}: {v}" for k, v in d["deny_reasons"].items()) or "none"
    chain = "intact" if verify_res.ok else f"BROKEN at seq {verify_res.first_broken_seq}"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>ToolGate dashboard</title>
<style>body{{font:14px system-ui;margin:24px;color:#1f2933}}table{{border-collapse:collapse}}
td,th{{border-bottom:1px solid #ddd;padding:6px 12px;text-align:right}}td:first-child,th:first-child{{text-align:left}}</style></head>
<body><h1>ToolGate</h1><p>Last {d["window_hours"]} h · {d["calls_per_min_last_10m"]} calls/min (10 min) ·
audit chain {chain} ({verify_res.checked} rows)</p>
<table><tr><th>Tool</th><th>Calls</th><th>Denied</th><th>Errors</th><th>Pending</th><th>p95 ms</th><th>LLM cost</th></tr>
{rows}</table><p>Deny reasons: {reasons}</p><p><a href="/metrics">Prometheus metrics</a></p></body></html>"""
