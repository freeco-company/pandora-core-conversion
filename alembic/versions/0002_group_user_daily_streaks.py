"""group_user_daily_streaks — group-level master streak (cross-App)

Idempotent on table presence: 0001 uses Base.metadata.create_all which means
on a fresh DB the new model's table is already created by 0001 (the squashed
initial migration). On an *existing* prod DB that's pinned at 0001 with a
real schema, this migration is the one that adds the table.

We use `inspect()` to skip the create when the table is already present so
both code paths converge.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_NAME = "group_user_daily_streaks"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE_NAME in inspect(bind).get_table_names():
        return
    op.create_table(
        TABLE_NAME,
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
    bind = op.get_bind()
    if TABLE_NAME not in inspect(bind).get_table_names():
        return
    op.drop_table(TABLE_NAME)
