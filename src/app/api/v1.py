"""Template demo endpoints. They exercise auth, validation, the queue and the LLM client;
replace them with each project's real routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.db import jobs
from app.adapters.llm import complete_with_retries
from app.api.auth import Principal, require_role
from app.api.deps import LLMDep, SessionDep, SettingsDep
from app.api.errors import DomainError

router = APIRouter(prefix="/v1", tags=["v1"])

Viewer = Annotated[Principal, Depends(require_role("viewer"))]
Clerk = Annotated[Principal, Depends(require_role("clerk"))]
Approver = Annotated[Principal, Depends(require_role("approver"))]


class WhoAmI(BaseModel):
    key_id: str
    role: str


@router.get("/whoami")
async def whoami(p: Viewer) -> WhoAmI:
    return WhoAmI(key_id=p.key_id, role=p.role)


class EnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["echo"]
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class EnqueueResponse(BaseModel):
    id: uuid.UUID
    created: bool


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(body: EnqueueRequest, session: SessionDep, _: Clerk) -> EnqueueResponse:
    job_id, created = await jobs.enqueue(
        session, body.kind, body.payload, idempotency_key=body.idempotency_key
    )
    return EnqueueResponse(id=job_id, created=created)


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, session: SessionDep, _: Viewer) -> dict[str, Any]:
    job = await jobs.get_job(session, job_id)
    if job is None:
        raise DomainError(404, "not-found", "Job not found")
    return job


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)


class CompleteResponse(BaseModel):
    text: str | None
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: str


@router.post("/complete")
async def complete(
    body: CompleteRequest, llm: LLMDep, settings: SettingsDep, _: Approver
) -> CompleteResponse:
    """Approver-only because it spends money."""
    res = await complete_with_retries(
        llm,
        settings.llm_max_attempts,
        prompt_version="template-complete-v1",
        messages=[{"role": "user", "content": body.prompt}],
    )
    return CompleteResponse(
        text=res.text,
        model=res.model,
        tokens_in=res.usage.tokens_in,
        tokens_out=res.usage.tokens_out,
        cost_usd=str(res.usage.cost_usd),
    )
