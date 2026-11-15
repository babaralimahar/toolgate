"""init: pgvector, api_keys, jobs, items

Revision ID: 0001
Revises:
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "role IN ('viewer', 'clerk', 'approver', 'admin')", name="ck_api_keys_role"
        ),
    )

    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("5")),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("locked_by", sa.String(64)),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'dead')", name="ck_jobs_status"
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts"),
    )
    # Partial indexes: the claim query only looks at queued jobs, the reaper at running ones.
    op.create_index(
        "ix_jobs_queued_run_after",
        "jobs",
        ["run_after"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_running_locked_at",
        "jobs",
        ["locked_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "items",
        sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(3)),
    )


def downgrade() -> None:
    op.drop_table("items")
    op.drop_index("ix_jobs_running_locked_at", table_name="jobs")
    op.drop_index("ix_jobs_queued_run_after", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("api_keys")
    op.execute("DROP EXTENSION IF EXISTS vector")
