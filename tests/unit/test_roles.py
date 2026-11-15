from __future__ import annotations

import pytest

from app.api.auth import require_role
from app.domain.roles import ROLES, has_role


def test_role_order() -> None:
    assert ROLES == ("viewer", "clerk", "approver", "admin")
    assert has_role("admin", "viewer")
    assert has_role("approver", "approver")
    assert not has_role("clerk", "approver")


def test_unknown_role_never_passes() -> None:
    assert not has_role("superuser", "viewer")


def test_require_unknown_role_fails_at_definition() -> None:
    with pytest.raises(ValueError):
        require_role("root")  # type: ignore[arg-type]
