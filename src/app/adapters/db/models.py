"""ORM models. Schema changes go through Alembic migrations, never ``create_all()``."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Identity, Integer, String, Text, false, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex, never the key
    role: Mapped[str] = mapped_column(String(20))
    disabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(server_default=text("5"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Item(Base):
    """Smoke-test table for pgvector. Real projects define their own embedding tables."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(3), nullable=True)
