"""Event outbox (Pub/Sub / BigQuery relay) and cross-application fingerprints.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BIGID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("topic", sa.String(60), nullable=False),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "id"])
    op.create_table(
        "fingerprints",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("owner_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fingerprints_case_id", "fingerprints", ["case_id"])
    op.create_index("ix_fingerprints_lookup", "fingerprints", ["kind", "value_hash"])


def downgrade() -> None:
    op.drop_table("fingerprints")
    op.drop_table("outbox_events")
