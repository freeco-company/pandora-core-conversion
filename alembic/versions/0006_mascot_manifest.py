"""mascot manifest (ADR-009 §2.1)

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gamification_mascot_manifest",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("species", sa.String(32), nullable=False),
        sa.Column("stage", sa.Integer, nullable=False),
        sa.Column("mood", sa.String(32), nullable=False),
        sa.Column("outfit_code", sa.String(64), nullable=False, server_default="none"),
        sa.Column("sprite_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("animation_url", sa.String(512), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "species", "stage", "mood", "outfit_code",
            name="uq_mascot_manifest_combo",
        ),
    )
    op.create_index(
        "ix_mascot_manifest_species",
        "gamification_mascot_manifest",
        ["species"],
    )


def downgrade() -> None:
    op.drop_index("ix_mascot_manifest_species", table_name="gamification_mascot_manifest")
    op.drop_table("gamification_mascot_manifest")
