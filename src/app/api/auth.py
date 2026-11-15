"""API-key authentication (who are you?) and role authorization (may you do this?)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends
from fastapi.security import APIKeyHeader

from app.adapters.db.api_keys import ApiKeyRepo, SqlApiKeyRepo, hash_key
from app.api.deps import SessionDep
from app.api.errors import DomainError
from app.domain.roles import ROLE_RANK, Role, has_role

# auto_error=False: we raise our own problem+json 401. Also gives Swagger an Authorize button.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    key_id: str
    role: str


async def get_api_key_repo(session: SessionDep) -> ApiKeyRepo:
    return SqlApiKeyRepo(session)


async def current_principal(
    repo: Annotated[ApiKeyRepo, Depends(get_api_key_repo)],
    x_api_key: Annotated[str | None, Depends(api_key_header)] = None,
) -> Principal:
    if not x_api_key:
        raise DomainError(401, "unauthenticated", "Missing API key")
    rec = await repo.find_by_hash(hash_key(x_api_key))
    if rec is None or rec.disabled:
        raise DomainError(401, "unauthenticated", "Invalid API key")
    structlog.contextvars.bind_contextvars(key_id=rec.id)
    return Principal(rec.id, rec.role)


def require_role(min_role: Role) -> Callable[..., Awaitable[Principal]]:
    if min_role not in ROLE_RANK:  # fail at import time, not at request time
        raise ValueError(f"unknown role {min_role!r}")

    async def dep(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not has_role(p.role, min_role):
            raise DomainError(403, "forbidden", "Insufficient role", f"requires {min_role}")
        return p

    return dep
