"""Initial production schema: cases, document blobs, audit log, accounts, jobs, rate limits.

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "cases",
        sa.Column("case_id", sa.String(40), primary_key=True),
        sa.Column("owner_uid", sa.String(120), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("business_name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("summary", JSON, nullable=False),
        sa.Column("document", JSON, nullable=False),
    )
    op.create_index("ix_cases_owner_uid", "cases", ["owner_uid"])
    op.create_index("ix_cases_status", "cases", ["status"])
    op.create_index("ix_cases_updated_at", "cases", ["updated_at"])

    op.create_table(
        "document_blobs",
        sa.Column("case_id", sa.String(40), primary_key=True),
        sa.Column("doc_id", sa.String(40), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("resource", sa.String(160), nullable=False),
        sa.Column("detail_hash", sa.String(64), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
    )

    op.create_table(
        "callback_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("uid", sa.String(120), nullable=False),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handled", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_callback_requests_case_id", "callback_requests", ["case_id"])

    op.create_table(
        "users",
        sa.Column("uid", sa.String(120), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "pipeline_jobs",
        sa.Column("job_id", sa.String(40), primary_key=True),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(80), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pipeline_jobs_case_id", "pipeline_jobs", ["case_id"])
    op.create_index("ix_pipeline_jobs_claim", "pipeline_jobs", ["state", "lease_until"])

    op.create_table(
        "rate_limits",
        sa.Column("bucket", sa.String(200), primary_key=True),
        sa.Column("window_start", sa.BigInteger(), primary_key=True),
        sa.Column("hits", sa.Integer(), nullable=False),
    )

    # The audit log is append-only in the database itself, not just by convention: rows cannot be
    # changed or removed by the application role, whatever the application code does.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION artha_audit_append_only() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'audit_log is append-only';
            END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute("""
            CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION artha_audit_append_only();
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
        op.execute("DROP FUNCTION IF EXISTS artha_audit_append_only()")
    for table in ("rate_limits", "pipeline_jobs", "users", "callback_requests", "audit_log", "document_blobs",
                  "cases"):
        op.drop_table(table)
