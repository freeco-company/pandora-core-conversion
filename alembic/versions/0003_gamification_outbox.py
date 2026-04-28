"""gamification webhook outbox (ADR-009 §2.2)

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gamification_outbox_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("pandora_user_uuid", UUID(as_uuid=True), nullable=False),
        sa.Column("consumer", sa.String(32), nullable=False),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "retry_count", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "event_id",
            "consumer",
            name="uq_gamification_outbox_event_consumer",
        ),
    )
    op.create_index(
        "ix_gamification_outbox_pending_due",
        "gamification_outbox_events",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "ix_gamification_outbox_event_type",
        "gamification_outbox_events",
        ["event_type"],
    )
    op.create_index(
        "ix_gamification_outbox_user",
        "gamification_outbox_events",
        ["pandora_user_uuid"],
    )
    op.create_index(
        "ix_gamification_outbox_consumer",
        "gamification_outbox_events",
        ["consumer"],
    )
    op.create_index(
        "ix_gamification_outbox_event_id",
        "gamification_outbox_events",
        ["event_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gamification_outbox_event_id", table_name="gamification_outbox_events")
    op.drop_index("ix_gamification_outbox_consumer", table_name="gamification_outbox_events")
    op.drop_index("ix_gamification_outbox_user", table_name="gamification_outbox_events")
    op.drop_index("ix_gamification_outbox_event_type", table_name="gamification_outbox_events")
    op.drop_index("ix_gamification_outbox_pending_due", table_name="gamification_outbox_events")
    op.drop_table("gamification_outbox_events")
