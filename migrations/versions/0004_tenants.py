"""Multi-tenancy: a lenders table, and the lender on every user and case.

Existing rows become the "default" lender, so a single-lender deployment keeps working unchanged.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tenants = op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(tenants, [{"tenant_id": "default", "name": "LedgerLens", "active": True,
                              "created_at": datetime.now(timezone.utc)}])
    for table in ("users", "cases"):
        op.add_column(table, sa.Column("tenant_id", sa.String(40), nullable=False, server_default="default"))
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])


def downgrade() -> None:
    for table in ("users", "cases"):
        op.drop_index(f"ix_{table}_tenant_id", table)
        op.drop_column(table, "tenant_id")
    op.drop_table("tenants")
