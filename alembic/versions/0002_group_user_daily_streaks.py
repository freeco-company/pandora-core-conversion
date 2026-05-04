"""group_user_daily_streaks — group-level master streak (cross-App)

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_user_daily_streaks",
        sa.Column("user_uuid", sa.CHAR(36), primary_key=True, nullable=False),
        sa.Column(
            "current_streak", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "longest_streak", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_login_date", sa.Date(), nullable=True),
        sa.Column("last_seen_app", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("group_user_daily_streaks")
