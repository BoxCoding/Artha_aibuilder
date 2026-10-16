"""Optional self-declared gender on staff and customer accounts (video guide presenter only).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("gender", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "gender")
