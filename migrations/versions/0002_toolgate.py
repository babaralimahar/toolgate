"""ToolGate: client identities, bound approvals, hash-chained append-only audit, kill switches.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gate_clients",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "name", sa.String(80), nullable=False, unique=True
        ),  # must exist in policies.yaml
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "gate_approvals",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("client", sa.String(80), nullable=False),
        sa.Column("tool", sa.String(80), nullable=False),
        sa.Column("args_hash", sa.String(64), nullable=False),
        sa.Column("args_preview", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(40)),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'used', 'expired')",
            name="ck_gate_approvals_status",
        ),
    )
    op.create_table(
        "gate_audit",
        sa.Column(
            "seq", sa.BigInteger, primary_key=True
        ),  # gapless: assigned under an advisory lock
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client", sa.String(80), nullable=False),
        sa.Column("tool", sa.String(80), nullable=False),
        sa.Column("args_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_gate_audit_ts", "gate_audit", ["ts"])
    # Append-only: updates, deletes and truncates are refused for everyone, owner included
    # (a superuser can still disable the trigger - which is what the hash chain is for).
    op.execute("""
    CREATE FUNCTION gate_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'gate_audit is append-only'; END $$
    """)
    op.execute(
        "CREATE TRIGGER gate_audit_no_update BEFORE UPDATE OR DELETE ON gate_audit FOR EACH ROW EXECUTE FUNCTION gate_audit_append_only()"
    )
    op.execute(
        "CREATE TRIGGER gate_audit_no_truncate BEFORE TRUNCATE ON gate_audit FOR EACH STATEMENT EXECUTE FUNCTION gate_audit_append_only()"
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON gate_audit FROM PUBLIC")
    op.create_table(
        "gate_kill_switches",
        sa.Column("scope", sa.String(100), primary_key=True),  # 'global' or 'tool:<name>'
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("updated_by", sa.String(40), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("gate_kill_switches")
    op.execute("DROP TABLE gate_audit")
    op.execute("DROP FUNCTION gate_audit_append_only()")
    op.drop_table("gate_approvals")
    op.drop_table("gate_clients")
