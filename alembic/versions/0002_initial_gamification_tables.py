"""initial gamification tables (ADR-009 §2.2)

Creates:
  - xp_ledger_entries (event-sourced XP ledger, idempotent per source_app)
  - user_progression  (snapshot for hot read)
  - gamification_achievements (catalog)
  - user_achievements (composite PK uuid+code)

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── xp_ledger_entries ────────────────────────────────────────────────
    op.create_table(
        "xp_ledger_entries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("pandora_user_uuid", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("source_app", sa.String(32), nullable=False),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("xp_delta", sa.Integer, nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "source_app",
            "idempotency_key",
            name="uq_xp_ledger_source_idempotency",
        ),
    )
    op.create_index(
        "ix_xp_ledger_user_kind_occurred",
        "xp_ledger_entries",
        ["pandora_user_uuid", "event_kind", "occurred_at"],
    )
    op.create_index("ix_xp_ledger_source_app", "xp_ledger_entries", ["source_app"])
    op.create_index("ix_xp_ledger_event_kind", "xp_ledger_entries", ["event_kind"])
    op.create_index("ix_xp_ledger_occurred_at", "xp_ledger_entries", ["occurred_at"])

    # ── user_progression ─────────────────────────────────────────────────
    op.create_table(
        "user_progression",
        sa.Column(
            "pandora_user_uuid",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("total_xp", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("group_level", sa.Integer, nullable=False, server_default="1"),
        sa.Column("level_anchor_xp", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("level_name_zh", sa.String(32), nullable=False, server_default="種子期"),
        sa.Column("level_name_en", sa.String(32), nullable=False, server_default="Seed"),
        sa.Column("last_level_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ── gamification_achievements ────────────────────────────────────────
    op.create_table(
        "gamification_achievements",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("source_app", sa.String(32), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("xp_reward", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_gamification_achievements_source_app",
        "gamification_achievements",
        ["source_app"],
    )

    # ── user_achievements ────────────────────────────────────────────────
    op.create_table(
        "user_achievements",
        sa.Column(
            "pandora_user_uuid",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "code",
            sa.String(64),
            sa.ForeignKey("gamification_achievements.code", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "awarded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("source_app", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_achievements")
    op.drop_index("ix_gamification_achievements_source_app", table_name="gamification_achievements")
    op.drop_table("gamification_achievements")
    op.drop_table("user_progression")
    op.drop_index("ix_xp_ledger_occurred_at", table_name="xp_ledger_entries")
    op.drop_index("ix_xp_ledger_event_kind", table_name="xp_ledger_entries")
    op.drop_index("ix_xp_ledger_source_app", table_name="xp_ledger_entries")
    op.drop_index("ix_xp_ledger_user_kind_occurred", table_name="xp_ledger_entries")
    op.drop_table("xp_ledger_entries")
