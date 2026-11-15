"""Typed settings, validated at startup (fail fast on missing or invalid values)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Accept the URL formats hosts hand out (postgres://, sslmode=require) and
    convert them to what SQLAlchemy's asyncpg driver expects."""
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":  # libpq name -> asyncpg name
            key = "ssl"
        if key == "channel_binding":  # libpq-only option; asyncpg rejects it
            continue
        query.append((key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ai-service-template"
    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Database
    database_url: str  # postgresql+asyncpg://user:pass@host:5432/db
    db_pool_size: int = Field(5, ge=1, le=50)
    db_max_overflow: int = Field(5, ge=0, le=50)

    # LLM
    llm_provider: Literal["fake", "openai_compat"] = "fake"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "set-me"  # record the exact id in docs/COSTS.md
    llm_timeout_s: float = Field(30.0, gt=0, le=120)
    llm_max_retries: int = Field(3, ge=0, le=5)  # retries after the first attempt
    pricing_path: Path = Path("config/pricing.yaml")

    # HTTP
    max_request_bytes: int = Field(10 * 1024 * 1024, ge=1024)

    # Observability
    otel_endpoint: str | None = None  # e.g. http://localhost:4318/v1/traces

    # Background jobs
    worker_mode: Literal["off", "embedded", "standalone"] = "embedded"
    worker_poll_s: float = Field(1.0, gt=0, le=60)
    job_lock_timeout_s: int = Field(300, ge=10)

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        v = normalize_database_url(v)
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return v

    @model_validator(mode="after")
    def _check_llm(self) -> Self:
        if self.llm_provider != "fake":
            if self.llm_api_key is None or not self.llm_api_key.get_secret_value():
                raise ValueError("LLM_API_KEY is required when LLM_PROVIDER is not 'fake'")
            if self.llm_model == "set-me":
                raise ValueError("LLM_MODEL must be set when LLM_PROVIDER is not 'fake'")
        return self

    @property
    def llm_max_attempts(self) -> int:
        return self.llm_max_retries + 1


@lru_cache
def get_settings() -> Settings:
    return Settings()  # raises at startup if required values are missing
