"""Roles, ordered from least to most privileged. The DB check constraint mirrors this list."""

from __future__ import annotations

from typing import Literal

Role = Literal["viewer", "clerk", "approver", "admin"]
ROLES: tuple[Role, ...] = ("viewer", "clerk", "approver", "admin")
ROLE_RANK: dict[str, int] = {role: rank for rank, role in enumerate(ROLES)}


def has_role(actual: str, required: Role) -> bool:
    """Unknown roles never pass."""
    return ROLE_RANK.get(actual, -1) >= ROLE_RANK[required]
