"""user-owned outfits (ADR-009 §6)

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gamification_user_outfits",
        sa.Column("pandora_user_uuid", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "code",
            sa.String(64),
            sa.ForeignKey("gamification_outfit_catalog.code", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "awarded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("awarded_via", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_gamification_user_outfits_user",
        "gamification_user_outfits",
        ["pandora_user_uuid"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gamification_user_outfits_user",
        table_name="gamification_user_outfits",
    )
    op.drop_table("gamification_user_outfits")
