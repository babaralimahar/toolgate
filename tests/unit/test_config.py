from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import normalize_database_url
from tests.conftest import make_settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+asyncpg://u:p@h/db?ssl=require"),
        (
            "postgresql://u:p@h/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@h/db?ssl=require",
        ),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ],
)
def test_database_url_normalized(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_non_postgres_url_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url="mysql://u:p@h/db")


def test_real_provider_requires_key_and_model() -> None:
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        make_settings(llm_provider="openai_compat", llm_model="m")
    with pytest.raises(ValidationError, match="LLM_MODEL"):
        make_settings(llm_provider="openai_compat", llm_api_key="sk-test")


def test_fake_provider_needs_no_key() -> None:
    s = make_settings()
    assert s.llm_provider == "fake"
    assert s.llm_max_attempts == s.llm_max_retries + 1


def test_api_key_is_secret_in_repr() -> None:
    s = make_settings(llm_provider="openai_compat", llm_api_key="sk-supersecret", llm_model="m")
    assert "sk-supersecret" not in repr(s)
