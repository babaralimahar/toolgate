"""API key storage. Only SHA-256 hashes are stored; the raw key is shown once at creation."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.db.models import ApiKey
from app.domain.roles import ROLES, Role


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    role: str
    disabled: bool


class ApiKeyRepo(Protocol):
    async def find_by_hash(self, key_hash: str) -> ApiKeyRecord | None: ...


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_key() -> tuple[str, str]:
    """Return (key_id, raw_key). The key id is a public handle; the raw key is the secret."""
    key_id = f"key_{secrets.token_hex(6)}"
    return key_id, f"sk_{secrets.token_urlsafe(32)}"


class SqlApiKeyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        row = await self._session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
        if row is None:
            return None
        return ApiKeyRecord(id=row.id, role=row.role, disabled=row.disabled)

    async def create(self, name: str, role: Role) -> tuple[str, str]:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        key_id, raw = generate_key()
        self._session.add(ApiKey(id=key_id, name=name, key_hash=hash_key(raw), role=role))
        await self._session.commit()
        return key_id, raw

    async def disable(self, key_id: str) -> bool:
        row = await self._session.get(ApiKey, key_id)
        if row is None:
            return False
        row.disabled = True
        await self._session.commit()
        return True
