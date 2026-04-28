"""gamification outfit catalog (ADR-009 §6)

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gamification_outfit_catalog",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "unlock_condition", sa.String(256), nullable=False, server_default=""
        ),
        sa.Column("tier", sa.String(32), nullable=False),
        sa.Column(
            "species_compat",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_gamification_outfit_catalog_tier",
        "gamification_outfit_catalog",
        ["tier"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_gamification_outfit_catalog_tier",
        table_name="gamification_outfit_catalog",
    )
    op.drop_table("gamification_outfit_catalog")
